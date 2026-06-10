package resolver

import (
	"bufio"
	"fmt"
	"os"
	"regexp"
	"sort"
	"strings"
	"unicode"

	"github.com/harneet2512/groundtruth/gt-index/internal/store"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// ---------------------------------------------------------------------------
// P0: Class Inheritance / Extends
// ---------------------------------------------------------------------------

// Regex patterns for extracting class inheritance across languages.
var (
	// Python: class Foo(Bar, Baz):
	pyClassRe = regexp.MustCompile(`^\s*class\s+(\w+)\s*\(([^)]+)\)\s*:`)
	// JS/TS: class Foo extends Bar
	jsExtendsRe = regexp.MustCompile(`class\s+(\w+)(?:\s*<[^>]*>)?\s+extends\s+(\w+)`)
	// Java/Kotlin: class Foo extends Bar
	javaExtendsRe = regexp.MustCompile(`class\s+(\w+)(?:\s*<[^>]*>)?\s+extends\s+(\w+)`)
	// Go embedded struct: a line inside a struct body that is just a type name (no field name)
	goEmbedRe = regexp.MustCompile(`^\s+(\*?)([A-Z]\w+)\s*$`)
)

// ---------------------------------------------------------------------------
// P1: Interface Implementation
// ---------------------------------------------------------------------------

var (
	// Java/TS: class Foo implements Bar, Baz
	implementsRe = regexp.MustCompile(`class\s+(\w+)(?:\s*<[^>]*>)?\s+(?:extends\s+\w+\s+)?implements\s+([^{]+)`)
	// Go: func NewFoo() MyInterface { return &myStruct{} }
	goReturnInterfaceRe = regexp.MustCompile(`func\s+\w+\([^)]*\)\s+(\w+)\s*\{`)
	// Go interface declaration opener: `type Reader interface {`
	goIfaceOpenRe = regexp.MustCompile(`^\s*type\s+(\w+)\s+interface\s*\{`)
	// Go embedded interface line: a bare type name on its own (e.g. `io.Reader` or
	// `Stringer`). Captures the trailing identifier (last path segment).
	goIfaceEmbedRe = regexp.MustCompile(`^\s*(?:[\w.]+\.)?([A-Z]\w*)\s*$`)
)

// ---------------------------------------------------------------------------
// P2: Decorator / Annotation — Route detection
// ---------------------------------------------------------------------------

var (
	// Python: @app.route("/path") or @router.get("/path")
	pyRouteDecoratorRe = regexp.MustCompile(`^\s*@(?:app|router|api)\.(get|post|put|delete|patch|route)\s*\(\s*["']([^"']+)["']`)
	// Java: @RequestMapping("/path"), @GetMapping("/path"), etc.
	javaRouteMappingRe = regexp.MustCompile(`@(?:Request|Get|Post|Put|Delete|Patch)Mapping\s*\(\s*(?:value\s*=\s*)?["']([^"']+)["']`)
)

// ---------------------------------------------------------------------------
// P3: Component Composition (JSX)
// ---------------------------------------------------------------------------

var (
	// JSX: <ComponentName ... or <ComponentName>
	jsxComponentRe = regexp.MustCompile(`<([A-Z]\w+)[\s/>]`)
)

// ---------------------------------------------------------------------------
// P4: Re-exports / Barrel Files
// ---------------------------------------------------------------------------

var (
	// JS/TS: export { Foo, Bar } from "./module"
	namedReExportRe = regexp.MustCompile(`export\s*\{[^}]*\}\s*from\s*["']([^"']+)["']`)
	// JS/TS: export * from "./module"
	starReExportRe = regexp.MustCompile(`export\s*\*\s*from\s*["']([^"']+)["']`)
)

// ResolveRelationships runs 5 extraction passes over already-indexed source
// files and inserts relationship edges (EXTENDS, IMPLEMENTS, HANDLES_ROUTE,
// COMPOSES, RE_EXPORTS) into graph.db. Returns the number of edges created.
func ResolveRelationships(db *store.DB, files []walker.SourceFile, root string) (int, error) {
	// Pre-build indexes from the DB: name -> []nodeID with label filter.
	classIndex, interfaceIndex, funcFileIndex := buildRelationshipIndexes(db)

	// File-path -> first node ID (for file-level anchoring of edges)
	fileNodeMap := buildFileNodeMap(db, files)

	var edges []*store.Edge
	seen := make(map[edgeKey]bool)

	addEdge := func(sourceID, targetID int64, edgeType, sourceFile string, sourceLine int, method string, confidence float64) {
		if sourceID == 0 || targetID == 0 || sourceID == targetID {
			return
		}
		key := edgeKey{sourceID: sourceID, targetID: targetID, typ: edgeType}
		if seen[key] {
			return
		}
		seen[key] = true
		// #2: relationship edges previously left TrustTier/EvidenceType/
		// VerificationStatus as Go zero values, and the explicit empty bind in
		// BatchInsertEdges defeats the SQL column DEFAULTs — every EXTENDS/
		// IMPLEMENTS/COMPOSES/RE_EXPORTS row landed with trust_tier='' and
		// verification_status=''. Stamp them here from the SAME thresholds
		// tierFor uses, so tier always follows confidence.
		edges = append(edges, &store.Edge{
			SourceID:           sourceID,
			TargetID:           targetID,
			Type:               edgeType,
			SourceLine:         sourceLine,
			SourceFile:         sourceFile,
			ResolutionMethod:   method,
			Confidence:         confidence,
			TrustTier:          tierFor(confidence),
			CandidateCount:     1,
			EvidenceType:       method,
			VerificationStatus: "unverified",
		})
	}

	// Go interfaces collected during the source scan, for CHA-style structural
	// method-set satisfaction (a struct whose method set covers an interface's
	// method set IMPLEMENTS it). Populated in the "go" case below; consumed after
	// the file loop by resolveGoImplements.
	var goInterfaces []goInterfaceDecl

	for _, sf := range files {
		absPath := sf.AbsPath
		if absPath == "" {
			absPath = root + "/" + sf.Path
		}

		f, err := os.Open(absPath)
		if err != nil {
			continue
		}

		scanner := bufio.NewScanner(f)
		scanner.Buffer(make([]byte, 1024*1024), 1024*1024) // 1MB buffer for long lines
		lineNum := 0
		pendingRoutePath := ""  // route path from decorator, waiting for the next def
		pendingRouteLine := 0   // line of the route decorator
		inStruct := false       // Go: tracking struct body for embedded types
		var currentStructName string
		var currentStructLine int

		// Go: tracking an interface body for CHA method-set collection.
		inInterface := false
		var currentIface goInterfaceDecl

		for scanner.Scan() {
			lineNum++
			line := scanner.Text()

			switch sf.Language {
			case "python":
				// P0: Python class inheritance
				if m := pyClassRe.FindStringSubmatch(line); m != nil {
					childName := m[1]
					baseList := m[2]
					childID := resolveClassNode(childName, sf.Path, classIndex)
					for _, base := range splitAndTrim(baseList) {
						// Skip known non-class bases
						if base == "" || base == "object" || base == "type" {
							continue
						}
						// Strip generic params like Base[T]
						if idx := strings.Index(base, "["); idx > 0 {
							base = base[:idx]
						}
						baseID := resolveClassNode(base, sf.Path, classIndex)
						if baseID != 0 && childID != 0 {
							addEdge(childID, baseID, "EXTENDS", sf.Path, lineNum, "inheritance", 1.0)
						}
					}
				}

				// P2: Python route decorators
				if m := pyRouteDecoratorRe.FindStringSubmatch(line); m != nil {
					pendingRoutePath = m[2]
					pendingRouteLine = lineNum
				}
				if pendingRoutePath != "" && strings.Contains(line, "def ") {
					// The function defined after the decorator handles the route.
					// Find the function name and create a HANDLES_ROUTE edge.
					defIdx := strings.Index(line, "def ")
					if defIdx >= 0 {
						rest := line[defIdx+4:]
						parenIdx := strings.Index(rest, "(")
						if parenIdx > 0 {
							funcName := strings.TrimSpace(rest[:parenIdx])
							if funcs := funcFileIndex[sf.Path]; funcs != nil {
								if funcID, ok := funcs[funcName]; ok {
									// Use file's first node as a pseudo "route" target
									fileID := fileNodeMap[sf.Path]
									if fileID != 0 {
										addEdge(funcID, fileID, "HANDLES_ROUTE", sf.Path, pendingRouteLine, "decorator_route", 0.95)
									}
								}
							}
						}
					}
					pendingRoutePath = ""
					pendingRouteLine = 0
				}

			case "javascript", "typescript":
				// P0: JS/TS class extends
				if m := jsExtendsRe.FindStringSubmatch(line); m != nil {
					childName := m[1]
					baseName := m[2]
					childID := resolveClassNode(childName, sf.Path, classIndex)
					baseID := resolveClassNode(baseName, sf.Path, classIndex)
					if childID != 0 && baseID != 0 {
						addEdge(childID, baseID, "EXTENDS", sf.Path, lineNum, "inheritance", 1.0)
					}
				}

				// P1: JS/TS implements
				if m := implementsRe.FindStringSubmatch(line); m != nil {
					childName := m[1]
					implList := m[2]
					childID := resolveClassNode(childName, sf.Path, classIndex)
					for _, iface := range splitAndTrim(implList) {
						if iface == "" {
							continue
						}
						// Strip generic params
						if idx := strings.Index(iface, "<"); idx > 0 {
							iface = iface[:idx]
						}
						ifaceID := resolveInterfaceOrClassNode(iface, sf.Path, interfaceIndex, classIndex)
						if childID != 0 && ifaceID != 0 {
							addEdge(childID, ifaceID, "IMPLEMENTS", sf.Path, lineNum, "implements", 1.0)
						}
					}
				}

				// P3: JSX component composition
				if matches := jsxComponentRe.FindAllStringSubmatch(line, -1); matches != nil {
					// Find the enclosing function/class for this file at this line
					sourceID := fileNodeMap[sf.Path]
					if funcID := findEnclosingFunc(sf.Path, lineNum, funcFileIndex); funcID != 0 {
						sourceID = funcID
					}
					for _, m := range matches {
						componentName := m[1]
						// Skip HTML-like names (all caps, single letter, or common HTML)
						if isHTMLElement(componentName) {
							continue
						}
						targetID := resolveClassOrFuncNode(componentName, sf.Path, classIndex, funcFileIndex)
						if targetID != 0 {
							addEdge(sourceID, targetID, "COMPOSES", sf.Path, lineNum, "jsx_component", 0.9)
						}
					}
				}

				// P4: Re-exports
				if m := namedReExportRe.FindStringSubmatch(line); m != nil {
					sourceModule := m[1]
					targetFile := resolveModuleToFile(sourceModule, sf.Path, files)
					if targetFile != "" {
						sourceID := fileNodeMap[sf.Path]
						targetID := fileNodeMap[targetFile]
						if sourceID != 0 && targetID != 0 {
							addEdge(sourceID, targetID, "RE_EXPORTS", sf.Path, lineNum, "re_export", 1.0)
						}
					}
				}
				if m := starReExportRe.FindStringSubmatch(line); m != nil {
					sourceModule := m[1]
					targetFile := resolveModuleToFile(sourceModule, sf.Path, files)
					if targetFile != "" {
						sourceID := fileNodeMap[sf.Path]
						targetID := fileNodeMap[targetFile]
						if sourceID != 0 && targetID != 0 {
							addEdge(sourceID, targetID, "RE_EXPORTS", sf.Path, lineNum, "re_export", 1.0)
						}
					}
				}

			case "java", "kotlin":
				// P0: Java/Kotlin extends
				if m := javaExtendsRe.FindStringSubmatch(line); m != nil {
					childName := m[1]
					baseName := m[2]
					childID := resolveClassNode(childName, sf.Path, classIndex)
					baseID := resolveClassNode(baseName, sf.Path, classIndex)
					if childID != 0 && baseID != 0 {
						addEdge(childID, baseID, "EXTENDS", sf.Path, lineNum, "inheritance", 1.0)
					}
				}

				// P1: Java/Kotlin implements
				if m := implementsRe.FindStringSubmatch(line); m != nil {
					childName := m[1]
					implList := m[2]
					childID := resolveClassNode(childName, sf.Path, classIndex)
					for _, iface := range splitAndTrim(implList) {
						if iface == "" {
							continue
						}
						if idx := strings.Index(iface, "<"); idx > 0 {
							iface = iface[:idx]
						}
						ifaceID := resolveInterfaceOrClassNode(iface, sf.Path, interfaceIndex, classIndex)
						if childID != 0 && ifaceID != 0 {
							addEdge(childID, ifaceID, "IMPLEMENTS", sf.Path, lineNum, "implements", 1.0)
						}
					}
				}

				// P2: Java route annotations — already handled by Pass 4b (API edges).
				// HANDLES_ROUTE edges for Java are skipped here to avoid duplication.

			case "go":
				// CHA: collect interface method sets. `type Name interface {` opens a
				// body whose member lines are required method signatures. We record the
				// method NAME of each (the matchable unit for structural satisfaction);
				// a bare type name inside the body is an embedded interface (recorded as
				// an embed to expand transitively after the scan).
				if m := goIfaceOpenRe.FindStringSubmatch(line); m != nil {
					inInterface = true
					currentIface = goInterfaceDecl{
						Name:     m[1],
						FilePath: sf.Path,
						Line:     lineNum,
					}
					// Single-line empty interface `type X interface {}` closes immediately.
					if strings.Contains(line[strings.Index(line, "interface"):], "}") {
						inInterface = false
						goInterfaces = append(goInterfaces, currentIface)
						currentIface = goInterfaceDecl{}
					}
				} else if inInterface {
					trimmed := strings.TrimSpace(line)
					if trimmed == "}" || strings.HasPrefix(trimmed, "}") {
						inInterface = false
						goInterfaces = append(goInterfaces, currentIface)
						currentIface = goInterfaceDecl{}
					} else if trimmed != "" && !strings.HasPrefix(trimmed, "//") {
						if sig, ok := parseGoMethodSig(trimmed); ok {
							// `Method(args) ret` — a required method with its
							// structural fingerprint (name + arity + result-presence).
							currentIface.Methods = append(currentIface.Methods, sig)
						} else if me := goIfaceEmbedRe.FindStringSubmatch(trimmed); me != nil {
							// A bare type name on its own line = embedded interface.
							// A QUALIFIED embed (`io.Reader`) names an interface in
							// another package — this regex layer cannot resolve which
							// one, and matching the bare tail name against a same-named
							// project interface would be a false expansion. #1c:
							// under-approximation must abstain, so mark INCOMPLETE.
							if strings.Contains(trimmed, ".") {
								currentIface.Incomplete = true
							} else {
								currentIface.Embeds = append(currentIface.Embeds, me[1])
							}
						} else {
							// #1c: a body line that parses as neither a method nor an
							// embed (multi-line declaration, type union, …) means the
							// required method set is UNKNOWN — mark the interface
							// incomplete so resolveGoImplements abstains.
							currentIface.Incomplete = true
						}
					}
				}

				// P0: Go embedded structs (inheritance-like)
				// Detect struct opening: type Foo struct {
				if !inInterface && strings.Contains(line, "struct") && strings.Contains(line, "{") {
					// Extract struct name
					parts := strings.Fields(line)
					for i, p := range parts {
						if p == "type" && i+1 < len(parts) {
							currentStructName = parts[i+1]
							currentStructLine = lineNum
							inStruct = true
							break
						}
					}
				}
				if inStruct {
					if strings.TrimSpace(line) == "}" {
						inStruct = false
						currentStructName = ""
					} else if m := goEmbedRe.FindStringSubmatch(line); m != nil {
						embeddedType := m[2]
						if currentStructName != "" {
							childID := resolveClassNode(currentStructName, sf.Path, classIndex)
							baseID := resolveClassNode(embeddedType, sf.Path, classIndex)
							if childID != 0 && baseID != 0 {
								addEdge(childID, baseID, "EXTENDS", sf.Path, currentStructLine, "inheritance", 1.0)
							}
						}
					}
				}

				// P1: Go — detect func returning interface type (simplified)
				if m := goReturnInterfaceRe.FindStringSubmatch(line); m != nil {
					returnType := m[1]
					// Only if the return type matches a known interface
					if ifaceID := resolveInterfaceNode(returnType, sf.Path, interfaceIndex); ifaceID != 0 {
						// Check if next few lines construct a struct
						// (simplified: just note the edge exists from this function to the interface)
						funcSourceID := fileNodeMap[sf.Path]
						if funcSourceID != 0 {
							addEdge(funcSourceID, ifaceID, "IMPLEMENTS", sf.Path, lineNum, "implements", 0.8)
						}
					}
				}

			case "rust":
				// Rust: impl Trait for Struct
				if strings.Contains(line, "impl ") && strings.Contains(line, " for ") {
					parts := strings.Fields(line)
					var traitName, structName string
					for i, p := range parts {
						if p == "impl" && i+1 < len(parts) {
							traitName = parts[i+1]
						}
						if p == "for" && i+1 < len(parts) {
							structName = strings.TrimSuffix(parts[i+1], "{")
							structName = strings.TrimSpace(structName)
						}
					}
					if traitName != "" && structName != "" {
						// Strip generic bounds: Trait<T> -> Trait
						if idx := strings.Index(traitName, "<"); idx > 0 {
							traitName = traitName[:idx]
						}
						if idx := strings.Index(structName, "<"); idx > 0 {
							structName = structName[:idx]
						}
						structID := resolveClassNode(structName, sf.Path, classIndex)
						traitID := resolveInterfaceOrClassNode(traitName, sf.Path, interfaceIndex, classIndex)
						if structID != 0 && traitID != 0 {
							addEdge(structID, traitID, "IMPLEMENTS", sf.Path, lineNum, "implements", 1.0)
						}
					}
				}
			}
		}
		// Flush an interface whose body was still open at EOF (no closing brace seen).
		if inInterface && currentIface.Name != "" {
			goInterfaces = append(goInterfaces, currentIface)
		}
		f.Close()
	}

	// CHA: Go structural method-set satisfaction. Emit IMPLEMENTS edges from each
	// struct whose method set covers an interface's required method set. Runs once
	// over the collected interfaces + the struct method sets read from the DB.
	resolveGoImplements(db, goInterfaces, classIndex, interfaceIndex, addEdge)

	if len(edges) == 0 {
		return 0, nil
	}

	if err := db.BatchInsertEdges(edges); err != nil {
		return 0, fmt.Errorf("insert relationship edges: %w", err)
	}

	return len(edges), nil
}

// ---------------------------------------------------------------------------
// Index builders
// ---------------------------------------------------------------------------

// classNodeEntry holds a class/struct node with its file and DB ID.
type classNodeEntry struct {
	Name     string
	FilePath string
	ID       int64
}

// goMethodSig is the structural fingerprint of one Go method usable at the
// regex extraction layer: name + parameter ARITY + result-presence. Full type
// equality is out of scope for a line regex; arity + result-presence kill the
// bulk of name-only false positives (#1a: `Close()` vs `Close() error` must
// not match). Parsed=false means the signature could not be extracted — the
// matcher must then abstain, never assume.
type goMethodSig struct {
	Name       string
	Arity      int
	HasResults bool
	Parsed     bool
}

// goInterfaceDecl is a Go interface collected during the source scan, holding the
// structural fingerprints of its required methods (the unit matched for CHA
// structural satisfaction) and any embedded interface names (expanded
// transitively before matching). Incomplete=true means at least one body line
// could not be classified (multi-line method, type union, qualified embed) —
// the required set is then an under-approximation and matching must abstain (#1c).
type goInterfaceDecl struct {
	Name       string
	FilePath   string
	Line       int
	Methods    []goMethodSig
	Embeds     []string
	Incomplete bool
}

// parseGoParamList scans a balanced parenthesized parameter list starting at
// s[0]=='(' and returns (arity, remainder-after-closing-paren, ok). Arity is
// the count of TOP-LEVEL commas + 1 for a non-empty list — grouped params
// (`a, b int`) intentionally count as their declared positions. Commas nested
// in parens/brackets/braces (func types, generics, struct literals) are not
// counted. ok=false on an unbalanced list (e.g. a declaration that spans
// lines) so the caller can abstain.
func parseGoParamList(s string) (int, string, bool) {
	if s == "" || s[0] != '(' {
		return 0, "", false
	}
	depthParen, depthBracket, depthBrace := 0, 0, 0
	commas := 0
	content := false
	for i := 0; i < len(s); i++ {
		switch s[i] {
		case '(':
			depthParen++
		case ')':
			depthParen--
			if depthParen == 0 {
				arity := 0
				if content {
					arity = commas + 1
				}
				return arity, s[i+1:], true
			}
		case '[':
			depthBracket++
		case ']':
			depthBracket--
		case '{':
			depthBrace++
		case '}':
			depthBrace--
		case ',':
			if depthParen == 1 && depthBracket == 0 && depthBrace == 0 {
				commas++
			}
		default:
			if depthParen >= 1 && s[i] != ' ' && s[i] != '\t' {
				content = true
			}
		}
	}
	return 0, "", false // unbalanced — multi-line declaration; abstain
}

// parseGoMethodSig parses a Go method signature fragment that STARTS at the
// method name — the form an interface body line takes (`Read(p []byte) (int,
// error)`). Returns ok=false when the fragment cannot be parsed confidently.
func parseGoMethodSig(s string) (goMethodSig, bool) {
	s = strings.TrimSpace(s)
	i := 0
	for i < len(s) {
		c := s[i]
		if c == '_' || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (i > 0 && c >= '0' && c <= '9') {
			i++
			continue
		}
		break
	}
	if i == 0 {
		return goMethodSig{}, false
	}
	name := s[:i]
	rest := strings.TrimSpace(s[i:])
	if rest == "" || rest[0] != '(' {
		return goMethodSig{}, false
	}
	arity, after, ok := parseGoParamList(rest)
	if !ok {
		return goMethodSig{}, false
	}
	// Strip a trailing line comment and the function BODY before checking for
	// declared results. Stored signatures are first-line slices, so a one-line
	// method carries its body (`Close() {}` / `Close() error { return nil }`) —
	// truncate at the first '{' (a Go result list never STARTS with '{').
	if ci := strings.Index(after, "//"); ci >= 0 {
		after = after[:ci]
	}
	if bi := strings.Index(after, "{"); bi >= 0 {
		after = after[:bi]
	}
	after = strings.TrimSpace(after)
	return goMethodSig{Name: name, Arity: arity, HasResults: after != "", Parsed: true}, true
}

// parseGoStructMethodSig parses the stored first-line signature of a Go method
// node (`func (f *FileX) Close() error {`) into the same structural fingerprint
// the interface side uses. ok=false when the signature is absent/unparseable.
func parseGoStructMethodSig(sig string) (goMethodSig, bool) {
	s := strings.TrimSpace(sig)
	if !strings.HasPrefix(s, "func") {
		return goMethodSig{}, false
	}
	s = strings.TrimSpace(strings.TrimPrefix(s, "func"))
	if s == "" {
		return goMethodSig{}, false
	}
	if s[0] == '(' {
		// Receiver — skip the balanced group.
		_, rest, ok := parseGoParamList(s)
		if !ok {
			return goMethodSig{}, false
		}
		s = strings.TrimSpace(rest)
	}
	return parseGoMethodSig(s)
}

// resolveGoImplements emits IMPLEMENTS edges via CHA-style structural method-set
// satisfaction: for every (struct, interface) pair where the struct's method set is
// a superset of the interface's required method set, the struct IMPLEMENTS the
// interface (Go has structural — not nominal — interface satisfaction, so there is
// no `implements` keyword to key on; the structural match IS the fact). Generalized:
// no per-repo logic, language-structural only.
//
// Struct method sets are read from the DB (methods are already parented to their
// Class/Struct node by the parser). Interface method sets come from the source scan.
// Non-empty interfaces only (the empty interface is satisfied by everything and is
// not a useful edge). Embedded interfaces are expanded transitively.
func resolveGoImplements(
	db *store.DB,
	interfaces []goInterfaceDecl,
	classIndex map[string][]classNodeEntry,
	interfaceIndex map[string][]classNodeEntry,
	addEdge func(sourceID, targetID int64, edgeType, sourceFile string, sourceLine int, method string, confidence float64),
) {
	if len(interfaces) == 0 {
		return
	}

	// structMethods: structNodeID -> its method fingerprints + the struct's file.
	structMethods := buildGoStructMethodSets(db)
	if len(structMethods) == 0 {
		return
	}

	// Index interfaces by bare name for embedded-interface expansion (a same-file
	// declaration is preferred at expansion time).
	ifaceByName := make(map[string][]*goInterfaceDecl, len(interfaces))
	for i := range interfaces {
		ifaceByName[interfaces[i].Name] = append(ifaceByName[interfaces[i].Name], &interfaces[i])
	}

	// #1d: key the expanded required sets by FILE+NAME, not name alone — two
	// same-named interfaces in different files must not collide onto one set.
	type requiredSet struct {
		methods  map[string]goMethodSig
		complete bool
	}
	keyOf := func(d *goInterfaceDecl) string { return d.FilePath + "\x00" + d.Name }
	required := make(map[string]requiredSet, len(interfaces))
	for i := range interfaces {
		d := &interfaces[i]
		k := keyOf(d)
		if _, done := required[k]; done {
			continue
		}
		methods, complete := expandGoInterfaceMethods(d, ifaceByName, make(map[string]bool))
		required[k] = requiredSet{methods: methods, complete: complete}
	}

	// Deterministic struct order so edge emission is stable across runs
	// (Go map iteration is randomized).
	structIDs := make([]int64, 0, len(structMethods))
	for id := range structMethods {
		structIDs = append(structIDs, id)
	}
	sort.Slice(structIDs, func(i, j int) bool { return structIDs[i] < structIDs[j] })

	// A struct satisfies an interface iff every required method matches by
	// NAME + ARITY + RESULT-PRESENCE (#1a). Name-only matching stamped every
	// struct with any `Close()` as a 0.95-CERTIFIED implementor of `Closer`.
	for i := range interfaces {
		iface := &interfaces[i]
		req := required[keyOf(iface)]
		if !req.complete {
			continue // #1c: under-approximated required set — abstain, never match
		}
		if len(req.methods) == 0 {
			continue // empty interface — satisfied by everything; not a useful edge
		}
		ifaceID := resolveInterfaceOrClassNode(iface.Name, iface.FilePath, interfaceIndex, classIndex)
		if ifaceID == 0 {
			continue
		}
		// #1b: name+arity+result-presence is still not full signature equality →
		// never 0.95/CERTIFIED. A ≥2-method match is strong structural evidence
		// (0.85, CANDIDATE); a 1-method interface is ambiguous by construction
		// (any struct with one matching method "implements" it) → 0.6, CANDIDATE.
		conf := 0.85
		if len(req.methods) == 1 {
			conf = 0.6
		}
		for _, structID := range structIDs {
			if structID == ifaceID {
				continue
			}
			sm := structMethods[structID]
			covers := true
			for name, want := range req.methods {
				have, ok := sm.Methods[name]
				if !ok || !have.Parsed || !want.Parsed ||
					have.Arity != want.Arity || have.HasResults != want.HasResults {
					covers = false
					break
				}
			}
			if covers {
				// #1e: source_file/line = the STRUCT's, so a `-file` reindex of the
				// struct's file deletes this edge along with the struct's nodes
				// (orphan-edge invariant); previously the edge carried the
				// interface's file and survived the struct's delete as an orphan.
				addEdge(structID, ifaceID, "IMPLEMENTS", sm.File, sm.Line, "structural_method_set_arity", conf)
			}
		}
	}
}

// goStructMethodSet is one struct's method fingerprints plus the struct node's
// own file/line (the source anchor CHA IMPLEMENTS edges must carry — #1e).
type goStructMethodSet struct {
	File    string
	Line    int
	Methods map[string]goMethodSig
}

// buildGoStructMethodSets reads the DB and returns structNodeID -> its method
// fingerprints, for Go Class/Struct nodes only. Methods are linked to their
// struct via parent_id (the parser parents Go receiver methods to their struct
// node). The join pulls the STRUCT's file/start_line so emitted edges anchor on
// the struct's file (#1e), and the method's stored signature so arity +
// result-presence can be verified (#1a).
func buildGoStructMethodSets(db *store.DB) map[int64]goStructMethodSet {
	out := make(map[int64]goStructMethodSet)

	tx, err := db.BeginTx()
	if err != nil {
		return out
	}
	defer tx.Rollback()

	// All Go method nodes with a non-zero parent (the parent is the struct).
	rows, err := tx.Query(`SELECT m.parent_id, m.name, COALESCE(m.signature, ''),
	        p.file_path, COALESCE(p.start_line, 0)
	   FROM nodes m JOIN nodes p ON m.parent_id = p.id
	  WHERE m.language = 'go' AND m.label = 'Method'
	    AND m.parent_id IS NOT NULL AND m.parent_id != 0`)
	if err != nil {
		return out
	}
	defer rows.Close()
	for rows.Next() {
		var parentID int64
		var name, sig, structFile string
		var structLine int
		if err := rows.Scan(&parentID, &name, &sig, &structFile, &structLine); err != nil {
			continue
		}
		entry, ok := out[parentID]
		if !ok {
			entry = goStructMethodSet{File: structFile, Line: structLine, Methods: make(map[string]goMethodSig)}
		}
		ms, parsed := parseGoStructMethodSig(sig)
		if !parsed || ms.Name != name {
			// Signature absent/unparseable: keep the name so the method is known
			// to exist, but Parsed=false makes the matcher abstain on it
			// (correct-or-quiet — never assume an arity we did not see).
			ms = goMethodSig{Name: name}
		}
		if _, dup := entry.Methods[name]; !dup {
			entry.Methods[name] = ms
		}
		out[parentID] = entry
	}
	return out
}

// expandGoInterfaceMethods returns the full required method set of an interface,
// transitively pulling in the methods of any embedded interfaces, plus a
// COMPLETE flag. complete=false when the declaration was marked Incomplete or
// any embed does not resolve to a project-local interface (#1c) — the caller
// must then abstain from emitting IMPLEMENTS for it (an under-approximated
// required set would match structs that do NOT implement the real interface).
// `visited` guards against cyclic embeds.
func expandGoInterfaceMethods(decl *goInterfaceDecl, byName map[string][]*goInterfaceDecl, visited map[string]bool) (map[string]goMethodSig, bool) {
	set := make(map[string]goMethodSig)
	key := decl.FilePath + "\x00" + decl.Name
	if visited[key] {
		return set, true // cycle — already being expanded higher in the stack
	}
	visited[key] = true
	if decl.Incomplete {
		return set, false
	}
	for _, m := range decl.Methods {
		set[m.Name] = m
	}
	for _, emb := range decl.Embeds {
		cands := byName[emb]
		if len(cands) == 0 {
			return set, false // #1c: embed is not project-local — required set unknown
		}
		// Prefer a same-file declaration; else the first collected (scan order).
		target := cands[0]
		for _, c := range cands {
			if c.FilePath == decl.FilePath {
				target = c
				break
			}
		}
		sub, complete := expandGoInterfaceMethods(target, byName, visited)
		if !complete {
			return set, false
		}
		for n, m := range sub {
			set[n] = m
		}
	}
	return set, true
}

// buildRelationshipIndexes queries graph.db for Class/Interface/Function nodes
// and returns lookup maps for the relationship extractor.
func buildRelationshipIndexes(db *store.DB) (
	classIndex map[string][]classNodeEntry,
	interfaceIndex map[string][]classNodeEntry,
	funcFileIndex map[string]map[string]int64,
) {
	classIndex = make(map[string][]classNodeEntry)
	interfaceIndex = make(map[string][]classNodeEntry)
	funcFileIndex = make(map[string]map[string]int64) // file -> funcName -> nodeID

	tx, err := db.BeginTx()
	if err != nil {
		return
	}
	defer tx.Rollback()

	// Class/Struct nodes
	rows, err := tx.Query(`SELECT id, name, file_path, label FROM nodes WHERE label IN ('Class', 'Struct', 'Interface', 'Enum', 'Type')`)
	if err != nil {
		return
	}
	for rows.Next() {
		var id int64
		var name, filePath, label string
		if err := rows.Scan(&id, &name, &filePath, &label); err != nil {
			continue
		}
		entry := classNodeEntry{Name: name, FilePath: filePath, ID: id}
		if label == "Interface" {
			interfaceIndex[name] = append(interfaceIndex[name], entry)
		} else {
			classIndex[name] = append(classIndex[name], entry)
		}
	}
	rows.Close()

	// Function/Method nodes for file-level lookup
	rows2, err := tx.Query(`SELECT id, name, file_path FROM nodes WHERE label IN ('Function', 'Method')`)
	if err != nil {
		return
	}
	for rows2.Next() {
		var id int64
		var name, filePath string
		if err := rows2.Scan(&id, &name, &filePath); err != nil {
			continue
		}
		if funcFileIndex[filePath] == nil {
			funcFileIndex[filePath] = make(map[string]int64)
		}
		funcFileIndex[filePath][name] = id
	}
	rows2.Close()

	return
}

// ---------------------------------------------------------------------------
// Resolution helpers
// ---------------------------------------------------------------------------

// resolveClassNode finds a Class/Struct node by name, preferring same-file.
func resolveClassNode(name, currentFile string, classIndex map[string][]classNodeEntry) int64 {
	entries := classIndex[name]
	if len(entries) == 0 {
		return 0
	}
	// Prefer same-file match
	for _, e := range entries {
		if e.FilePath == currentFile {
			return e.ID
		}
	}
	// Fall back to first match
	return entries[0].ID
}

// resolveInterfaceNode finds an Interface node by name.
func resolveInterfaceNode(name, currentFile string, interfaceIndex map[string][]classNodeEntry) int64 {
	entries := interfaceIndex[name]
	if len(entries) == 0 {
		return 0
	}
	for _, e := range entries {
		if e.FilePath == currentFile {
			return e.ID
		}
	}
	return entries[0].ID
}

// resolveInterfaceOrClassNode tries interface first, then class.
func resolveInterfaceOrClassNode(name, currentFile string, interfaceIndex, classIndex map[string][]classNodeEntry) int64 {
	if id := resolveInterfaceNode(name, currentFile, interfaceIndex); id != 0 {
		return id
	}
	return resolveClassNode(name, currentFile, classIndex)
}

// resolveClassOrFuncNode tries class index first, then function.
func resolveClassOrFuncNode(name, currentFile string, classIndex map[string][]classNodeEntry, funcFileIndex map[string]map[string]int64) int64 {
	if id := resolveClassNode(name, currentFile, classIndex); id != 0 {
		return id
	}
	// Search all files for a function with this name
	for _, funcs := range funcFileIndex {
		if id, ok := funcs[name]; ok {
			return id
		}
	}
	return 0
}

// findEnclosingFunc returns a function node in the file to use as the source
// for a JSX composition edge. Simplified: returns the first function in the file.
func findEnclosingFunc(filePath string, _ int, funcFileIndex map[string]map[string]int64) int64 {
	funcs := funcFileIndex[filePath]
	if len(funcs) == 0 {
		return 0
	}
	// Return any function in this file (we don't have start_line in the index)
	for _, id := range funcs {
		return id
	}
	return 0
}

// ---------------------------------------------------------------------------
// Module/file resolution for re-exports
// ---------------------------------------------------------------------------

// resolveModuleToFile resolves a relative module path (e.g. "./utils") to a
// file path that exists in the indexed file set.
func resolveModuleToFile(modulePath, currentFile string, files []walker.SourceFile) string {
	if modulePath == "" {
		return ""
	}

	// Compute the directory of the current file
	dir := ""
	if idx := strings.LastIndex(currentFile, "/"); idx >= 0 {
		dir = currentFile[:idx]
	}

	// Build candidate paths from the relative module path
	rel := modulePath
	if strings.HasPrefix(rel, "./") {
		rel = rel[2:]
	} else if strings.HasPrefix(rel, "../") {
		// Go up one directory
		if didx := strings.LastIndex(dir, "/"); didx >= 0 {
			dir = dir[:didx]
		} else {
			dir = ""
		}
		rel = rel[3:]
	} else {
		// Non-relative (bare specifier) — skip for barrel file detection
		return ""
	}

	var base string
	if dir != "" {
		base = dir + "/" + rel
	} else {
		base = rel
	}

	// Try common extensions
	exts := []string{"", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.tsx", "/index.js", "/index.jsx"}
	fileSet := make(map[string]bool, len(files))
	for _, f := range files {
		fileSet[f.Path] = true
	}

	for _, ext := range exts {
		candidate := base + ext
		if fileSet[candidate] {
			return candidate
		}
	}
	return ""
}

// ---------------------------------------------------------------------------
// Utility functions
// ---------------------------------------------------------------------------

// splitAndTrim splits a comma-separated list and trims whitespace from each entry.
func splitAndTrim(s string) []string {
	parts := strings.Split(s, ",")
	result := make([]string, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p != "" {
			result = append(result, p)
		}
	}
	return result
}

// isHTMLElement returns true if name looks like a standard HTML element name
// (even though it starts with uppercase in the regex, some false positives).
func isHTMLElement(name string) bool {
	// All standard React component names start with uppercase.
	// If it's a short name (< 3 chars), likely noise.
	if len(name) < 2 {
		return true
	}
	// If entirely uppercase and short, might be a constant/acronym, skip it.
	if len(name) <= 3 {
		allUpper := true
		for _, r := range name {
			if !unicode.IsUpper(r) {
				allUpper = false
				break
			}
		}
		if allUpper {
			return true
		}
	}
	return false
}


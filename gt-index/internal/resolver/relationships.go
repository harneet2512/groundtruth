package resolver

import (
	"bufio"
	"fmt"
	"os"
	"regexp"
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
		edges = append(edges, &store.Edge{
			SourceID:         sourceID,
			TargetID:         targetID,
			Type:             edgeType,
			SourceLine:       sourceLine,
			SourceFile:       sourceFile,
			ResolutionMethod: method,
			Confidence:       confidence,
		})
	}

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
				// P0: Go embedded structs (inheritance-like)
				// Detect struct opening: type Foo struct {
				if strings.Contains(line, "struct") && strings.Contains(line, "{") {
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
		f.Close()
	}

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


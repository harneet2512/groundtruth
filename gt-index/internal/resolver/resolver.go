// Package resolver resolves call references to definition nodes.
package resolver

import (
	"path/filepath"
	"strings"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

// ResolvedCall is a call reference that has been resolved to a target node.
type ResolvedCall struct {
	SourceNodeID int64
	TargetNodeID int64
	SourceLine   int
	SourceFile   string
	Method       string // "same_file", "import", "name_match"
}

// Resolve takes all call refs and all defined nodes, and resolves calls to definitions.
// Resolution strategies (in priority order):
//  1. Same-file exact name match → "same_file"
//  2. Import-verified cross-file → "import" (NEW in v13)
//  3. Cross-file name match → "name_match" (fallback, unreliable)
func Resolve(
	allCalls []parser.CallRef,
	nodeIDs map[string][]int64, // name → list of node IDs
	fileNodeIDs map[string]map[string]int64, // file → name → node ID
	callerNodeIDs []int64, // parallel to allCalls
	allImports []parser.ImportRef, // all parsed import statements
	fileMap map[string][]string, // module path → list of file paths
) []ResolvedCall {
	// Build import index: file → imported name → list of candidate target files
	importIndex := buildImportIndex(allImports, fileMap)

	var resolved []ResolvedCall

	for i, call := range allCalls {
		callerID := callerNodeIDs[i]
		if callerID == 0 {
			continue
		}

		calleeName := call.CalleeName

		// Strategy 1: Same-file exact name match
		if fileNodes, ok := fileNodeIDs[call.File]; ok {
			if targetID, ok := fileNodes[calleeName]; ok && targetID != callerID {
				resolved = append(resolved, ResolvedCall{
					SourceNodeID: callerID,
					TargetNodeID: targetID,
					SourceLine:   call.Line,
					SourceFile:   call.File,
					Method:       "same_file",
				})
				continue
			}
		}

		// Strategy 1.5: Import-verified cross-file resolution
		// If the caller file imports calleeName, resolve to the definition in the imported module.
		if fileImports, ok := importIndex[call.File]; ok {
			if candidateFiles, ok := fileImports[calleeName]; ok {
				found := false
				for _, targetFile := range candidateFiles {
					if fileNodes, ok := fileNodeIDs[targetFile]; ok {
						if targetID, ok := fileNodes[calleeName]; ok && targetID != callerID {
							resolved = append(resolved, ResolvedCall{
								SourceNodeID: callerID,
								TargetNodeID: targetID,
								SourceLine:   call.Line,
								SourceFile:   call.File,
								Method:       "import",
							})
							found = true
							break
						}
					}
				}
				if found {
					continue
				}
			}

			// Also check wildcard imports ("*") — if the caller file wildcard-imports a module
			// and that module has a definition for calleeName
			if candidateFiles, ok := fileImports["*"]; ok {
				found := false
				for _, targetFile := range candidateFiles {
					if fileNodes, ok := fileNodeIDs[targetFile]; ok {
						if targetID, ok := fileNodes[calleeName]; ok && targetID != callerID {
							resolved = append(resolved, ResolvedCall{
								SourceNodeID: callerID,
								TargetNodeID: targetID,
								SourceLine:   call.Line,
								SourceFile:   call.File,
								Method:       "import",
							})
							found = true
							break
						}
					}
				}
				if found {
					continue
				}
			}
		}

		// Strategy 2: Cross-file name match (fallback — unreliable)
		if targets, ok := nodeIDs[calleeName]; ok {
			for _, targetID := range targets {
				if targetID != callerID {
					resolved = append(resolved, ResolvedCall{
						SourceNodeID: callerID,
						TargetNodeID: targetID,
						SourceLine:   call.Line,
						SourceFile:   call.File,
						Method:       "name_match",
					})
					break // take first match
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

	for _, imp := range imports {
		if imp.ImportedName == "" {
			continue
		}

		fileEntry, ok := index[imp.File]
		if !ok {
			fileEntry = make(map[string][]string)
			index[imp.File] = fileEntry
		}

		// Resolve the module path to actual files
		targetFiles := resolveModulePath(imp.ModulePath, fileMap)

		// If module path didn't resolve, try module_path + imported_name
		// This handles Python: "from qutebrowser.browser import browsertab"
		// where module_path="qutebrowser.browser" doesn't map to a file,
		// but "qutebrowser.browser.browsertab" maps to qutebrowser/browser/browsertab.py
		if len(targetFiles) == 0 && imp.ImportedName != "*" && imp.ModulePath != "" {
			combined := imp.ModulePath + "." + imp.ImportedName
			targetFiles = resolveModulePath(combined, fileMap)
			if len(targetFiles) == 0 {
				// Also try slash form: module/name
				combinedSlash := strings.ReplaceAll(imp.ModulePath, ".", "/") + "/" + imp.ImportedName
				targetFiles = resolveModulePath(combinedSlash, fileMap)
			}
		}

		if len(targetFiles) > 0 {
			fileEntry[imp.ImportedName] = append(fileEntry[imp.ImportedName], targetFiles...)
		}
	}

	return index
}

// resolveModulePath maps a module path string to actual source file paths.
// Returns all matching files.
func resolveModulePath(modulePath string, fileMap map[string][]string) []string {
	if modulePath == "" {
		return nil
	}

	// Direct lookup (exact match)
	if files, ok := fileMap[modulePath]; ok {
		return files
	}

	// Try normalized forms
	normalized := strings.ReplaceAll(modulePath, ".", "/")
	if files, ok := fileMap[normalized]; ok {
		return files
	}

	// For relative imports (JS/TS): strip leading ./
	cleaned := strings.TrimPrefix(modulePath, "./")
	cleaned = strings.TrimPrefix(cleaned, "../")
	if files, ok := fileMap[cleaned]; ok {
		return files
	}

	// Try suffix match: if modulePath is "foo.bar", try matching files ending in "foo/bar.py" etc.
	// This handles Python imports where the project root varies
	var matches []string
	for key, files := range fileMap {
		if strings.HasSuffix(key, "/"+cleaned) || strings.HasSuffix(key, "/"+normalized) || key == cleaned || key == normalized {
			matches = append(matches, files...)
		}
	}

	return matches
}

// BuildNameIndex creates a map from symbol name to list of node IDs.
func BuildNameIndex(db *store.DB, nodes []store.Node, nodeDBIDs []int64) (map[string][]int64, map[string]map[string]int64) {
	nameIndex := make(map[string][]int64)
	fileIndex := make(map[string]map[string]int64)

	for i, n := range nodes {
		dbID := nodeDBIDs[i]
		nameIndex[n.Name] = append(nameIndex[n.Name], dbID)

		if _, ok := fileIndex[n.FilePath]; !ok {
			fileIndex[n.FilePath] = make(map[string]int64)
		}
		fileIndex[n.FilePath][n.Name] = dbID
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
				register(filepath.ToSlash(dir), filePath)
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

		case "java":
			// Java: src/main/java/com/foo/Bar.java → "com.foo.Bar", "com.foo", "foo.Bar"
			slashPath := filepath.ToSlash(filePath)
			// Strip common Java source roots
			for _, root := range []string{"src/main/java/", "src/test/java/", "src/"} {
				if strings.HasPrefix(slashPath, root) {
					slashPath = strings.TrimPrefix(slashPath, root)
					break
				}
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
			slashPath = strings.TrimPrefix(slashPath, "src/")
			noExt2 := strings.TrimSuffix(slashPath, ext)
			if stem == "mod" || stem == "lib" || stem == "main" {
				noExt2 = filepath.ToSlash(filepath.Dir(slashPath))
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
		}
	}

	return fm
}

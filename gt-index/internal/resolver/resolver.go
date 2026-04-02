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
	Method       string  // "same_file", "import", "name_match"
	Confidence   float64 // 0.0–1.0
}

// edgeKey is used for deduplication.
type edgeKey struct {
	sourceID int64
	targetID int64
	typ      string
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
	seen := make(map[edgeKey]bool) // deduplication

	for i, call := range allCalls {
		callerID := callerNodeIDs[i]
		if callerID == 0 {
			continue
		}

		calleeName := call.CalleeName

		// Strategy 1: Same-file exact name match
		if fileNodes, ok := fileNodeIDs[call.File]; ok {
			if targetID, ok := fileNodes[calleeName]; ok && targetID != callerID {
				key := edgeKey{callerID, targetID, "CALLS"}
				if !seen[key] {
					seen[key] = true
					resolved = append(resolved, ResolvedCall{
						SourceNodeID: callerID,
						TargetNodeID: targetID,
						SourceLine:   call.Line,
						SourceFile:   call.File,
						Method:       "same_file",
						Confidence:   1.0,
					})
				}
				continue
			}
		}

		// Strategy 1.5: Import-verified cross-file resolution
		if fileImports, ok := importIndex[call.File]; ok {
			if candidateFiles, ok := fileImports[calleeName]; ok {
				found := false
				for _, targetFile := range candidateFiles {
					if fileNodes, ok := fileNodeIDs[targetFile]; ok {
						if targetID, ok := fileNodes[calleeName]; ok && targetID != callerID {
							key := edgeKey{callerID, targetID, "CALLS"}
							if !seen[key] {
								seen[key] = true
								resolved = append(resolved, ResolvedCall{
									SourceNodeID: callerID,
									TargetNodeID: targetID,
									SourceLine:   call.Line,
									SourceFile:   call.File,
									Method:       "import",
									Confidence:   1.0,
								})
							}
							found = true
							break
						}
					}
				}
				if found {
					continue
				}
			}

			// Wildcard imports
			if candidateFiles, ok := fileImports["*"]; ok {
				found := false
				for _, targetFile := range candidateFiles {
					if fileNodes, ok := fileNodeIDs[targetFile]; ok {
						if targetID, ok := fileNodes[calleeName]; ok && targetID != callerID {
							key := edgeKey{callerID, targetID, "CALLS"}
							if !seen[key] {
								seen[key] = true
								resolved = append(resolved, ResolvedCall{
									SourceNodeID: callerID,
									TargetNodeID: targetID,
									SourceLine:   call.Line,
									SourceFile:   call.File,
									Method:       "import",
									Confidence:   1.0,
								})
							}
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

		// Strategy 2: Cross-file name match (fallback)
		// Collect all candidates and pick the best one (prefer same directory)
		if targets, ok := nodeIDs[calleeName]; ok {
			candidateCount := 0
			var bestTarget int64
			bestScore := -1

			callerDir := filepath.Dir(call.File)

			for _, targetID := range targets {
				if targetID == callerID {
					continue
				}
				candidateCount++

				// Score: prefer same directory, then any match
				score := 0
				// We don't have target file path in nodeIDs, so use first valid candidate
				// In future, store file paths in the index for better scoring
				if bestTarget == 0 {
					bestTarget = targetID
					bestScore = score
				}
			}

			if bestTarget != 0 {
				key := edgeKey{callerID, bestTarget, "CALLS"}
				if !seen[key] {
					seen[key] = true
					resolved = append(resolved, ResolvedCall{
						SourceNodeID: callerID,
						TargetNodeID: bestTarget,
						SourceLine:   call.Line,
						SourceFile:   call.File,
						Method:       "name_match",
						Confidence:   computeConfidence("name_match", candidateCount),
					})
				}
			}
			_ = bestScore // used for future directory-based scoring
			_ = callerDir
		}
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

		// Resolve the module path to actual files (cached)
		targetFiles, cached := moduleCache[imp.ModulePath]
		if !cached {
			targetFiles = resolveModulePath(imp.ModulePath, fileMap)
			moduleCache[imp.ModulePath] = targetFiles
		}

		// If module path didn't resolve, try module_path + imported_name (cached)
		if len(targetFiles) == 0 && imp.ImportedName != "*" && imp.ModulePath != "" {
			combined := imp.ModulePath + "." + imp.ImportedName
			if cached, ok := moduleCache[combined]; ok {
				targetFiles = cached
			} else {
				targetFiles = resolveModulePath(combined, fileMap)
				moduleCache[combined] = targetFiles
			}
			if len(targetFiles) == 0 {
				combinedSlash := strings.ReplaceAll(imp.ModulePath, ".", "/") + "/" + imp.ImportedName
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
	if cleaned != modulePath {
		if files, ok := fileMap[cleaned]; ok {
			return files
		}
	}

	// No linear scan — BuildFileMap already registers suffix variants
	// so all lookups above should catch them. Return nil if nothing matched.
	return nil
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

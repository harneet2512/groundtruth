// Package resolver resolves call references to definition nodes.
//
// Resolution strategy (deterministic, ordered by confidence):
//
//   Stage 1: Same-file resolution (confidence 1.0)
//     Callee name matches a definition in the same file as the call site.
//
//   Stage 2: Import-verified resolution (confidence 1.0)
//     Call target traced through an explicit import statement.
//     Handles both direct imports ("from X import Y" → Y()) and
//     qualified calls through package/module imports ("import fmt" → fmt.Printf()).
//     Language-specific: Python (from X import Y), Go (import "pkg"), Java (import pkg.Class), etc.
//     Requires an import extractor for the language (17 languages supported).
//
//   Stage 3: Name-match fallback (confidence 0.2–0.9)
//     Call target matched by function name across all indexed files.
//     Confidence is tiered by ambiguity: 1 candidate → 0.9, 2 → 0.6, 3–5 → 0.4, 5+ → 0.2.
//     Only Stage 1 and 2 edges are used for high-confidence evidence delivery.
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
		// Handles both:
		//   (a) Direct symbol imports: "from os.path import join" → join()
		//   (b) Qualified calls through package/module imports: "import fmt" → fmt.Printf()
		if fileImports, ok := importIndex[call.File]; ok {
			var importCandidates []int64

			// (a) Check direct symbol imports: importedName matches calleeName
			if candidateFiles, ok := fileImports[calleeName]; ok {
				for _, targetFile := range candidateFiles {
					if fileNodes, ok := fileNodeIDs[targetFile]; ok {
						if targetID, ok := fileNodes[calleeName]; ok && targetID != callerID {
							importCandidates = append(importCandidates, targetID)
						}
					}
				}
				// Re-export fallback: if the target file is a package __init__.py or index.js
				// and the symbol wasn't found there, search sibling files in the same directory.
				// Handles: "from flask import Flask" where Flask is in flask/app.py, not flask/__init__.py
				if len(importCandidates) == 0 {
					for _, targetFile := range candidateFiles {
						// Normalize to forward slashes (Windows filepath.Dir uses backslashes)
						targetDir := filepath.ToSlash(filepath.Dir(targetFile)) + "/"
						for otherFile, fileNodes := range fileNodeIDs {
							if otherFile != targetFile && strings.HasPrefix(otherFile, targetDir) {
								if targetID, ok := fileNodes[calleeName]; ok && targetID != callerID {
									importCandidates = append(importCandidates, targetID)
								}
							}
						}
					}
				}
			}

			// (b) Qualified call resolution: "fmt.Printf" → qualifier="fmt", symbol="Printf"
			//     Look up qualifier in imports → get candidate files → find symbol there.
			//     Handles: Go (fmt.Printf), JS (utils.helper), Ruby (Foo.bar),
			//     PHP ($obj->method), Lua (mod.func), C++ (ns::func)
			if len(importCandidates) == 0 && call.CalleeQualified != "" && call.CalleeQualified != calleeName {
				qualifier, symbol := splitQualifiedCall(call.CalleeQualified)
				if qualifier != "" && symbol != "" {
					if candidateFiles, ok := fileImports[qualifier]; ok {
						for _, targetFile := range candidateFiles {
							if fileNodes, ok := fileNodeIDs[targetFile]; ok {
								if targetID, ok := fileNodes[symbol]; ok && targetID != callerID {
									importCandidates = append(importCandidates, targetID)
								}
							}
						}
					}
					// Also try: qualifier might be a class, symbol a method.
					// Look up qualifier as a node name in candidate files, then
					// check if symbol is defined as a child of that class.
					if len(importCandidates) == 0 {
						if candidateFiles, ok := fileImports[qualifier]; ok {
							for _, targetFile := range candidateFiles {
								if fileNodes, ok := fileNodeIDs[targetFile]; ok {
									// Try the symbol directly (method defined at file level)
									if targetID, ok := fileNodes[symbol]; ok && targetID != callerID {
										importCandidates = append(importCandidates, targetID)
									}
								}
							}
						}
						// Try qualifier with wildcard (package-level import)
						if len(importCandidates) == 0 {
							if candidateFiles, ok := fileImports["*"]; ok {
								for _, targetFile := range candidateFiles {
									if fileNodes, ok := fileNodeIDs[targetFile]; ok {
										if targetID, ok := fileNodes[symbol]; ok && targetID != callerID {
											importCandidates = append(importCandidates, targetID)
										}
										if targetID, ok := fileNodes[calleeName]; ok && targetID != callerID {
											importCandidates = append(importCandidates, targetID)
										}
									}
								}
							}
						}
						// Re-export fallback for qualified calls: if qualifier maps to
						// a package __init__.py and symbol wasn't found there, search
						// sibling files in the same directory.
						// Handles: "import astropy.units as u" then "u.Quantity()"
						// where Quantity is in quantity.py, not __init__.py
						if len(importCandidates) == 0 {
							if candidateFiles, ok := fileImports[qualifier]; ok {
								for _, targetFile := range candidateFiles {
									targetDir := filepath.ToSlash(filepath.Dir(targetFile)) + "/"
									for otherFile, fileNodes := range fileNodeIDs {
										if otherFile != targetFile && strings.HasPrefix(otherFile, targetDir) {
											if targetID, ok := fileNodes[symbol]; ok && targetID != callerID {
												importCandidates = append(importCandidates, targetID)
											}
										}
									}
								}
							}
						}
					}
				}
			}

			// (c) Check wildcard imports for unqualified calls
			if len(importCandidates) == 0 {
				if candidateFiles, ok := fileImports["*"]; ok {
					for _, targetFile := range candidateFiles {
						if fileNodes, ok := fileNodeIDs[targetFile]; ok {
							if targetID, ok := fileNodes[calleeName]; ok && targetID != callerID {
								importCandidates = append(importCandidates, targetID)
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
						SourceNodeID: callerID,
						TargetNodeID: bestTarget,
						SourceLine:   call.Line,
						SourceFile:   call.File,
						Method:       "import",
						Confidence:   1.0,
					})
				}
				continue
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

// splitQualifiedCall splits a qualified call like "fmt.Printf" into ("fmt", "Printf").
// Handles multiple separator conventions:
//   - Dot:        obj.method, module.func (Python, JS, Go, Ruby, Lua, Java, PHP)
//   - Double colon: Foo::bar (Rust, C++, Ruby, Scala)
//   - Arrow:      $obj->method (PHP)
//
// Returns the first qualifier and the final symbol. For chains like a.b.c(),
// returns ("a", "c") — the qualifier is the import name, the symbol is the method.
func splitQualifiedCall(qualified string) (string, string) {
	// Try separators in order of specificity
	// "::" first (Rust, C++, Scala) — more specific than "."
	if idx := strings.Index(qualified, "::"); idx >= 0 {
		qualifier := qualified[:idx]
		rest := qualified[idx+2:]
		// For chains like a::b::c, qualifier is first component, symbol is last
		if lastIdx := strings.LastIndex(rest, "::"); lastIdx >= 0 {
			return qualifier, rest[lastIdx+2:]
		}
		return qualifier, rest
	}
	// "->" (PHP)
	if idx := strings.Index(qualified, "->"); idx >= 0 {
		qualifier := qualified[:idx]
		// Strip PHP $ prefix: $this->method → qualifier="this"
		qualifier = strings.TrimPrefix(qualifier, "$")
		rest := qualified[idx+2:]
		if lastIdx := strings.LastIndex(rest, "->"); lastIdx >= 0 {
			return qualifier, rest[lastIdx+2:]
		}
		return qualifier, rest
	}
	// "." (most languages)
	if idx := strings.Index(qualified, "."); idx >= 0 {
		qualifier := qualified[:idx]
		rest := qualified[idx+1:]
		// For chains like a.b.c, symbol is last component
		if lastIdx := strings.LastIndex(rest, "."); lastIdx >= 0 {
			return qualifier, rest[lastIdx+1:]
		}
		return qualifier, rest
	}
	return "", ""
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

		// Resolve relative Python imports (leading dots) to absolute module paths.
		// "from .foo import Bar" in file "pkg/sub/mod.py" → module = "pkg.sub.foo"
		// "from ..utils import baz" in file "pkg/sub/mod.py" → module = "pkg.utils"
		// "from . import coords" in file "pkg/mod.py" → module = "pkg.coords"
		effectiveModulePath := imp.ModulePath
		if strings.HasPrefix(effectiveModulePath, ".") {
			effectiveModulePath = resolvePythonRelativeImport(effectiveModulePath, imp.File)
		}

		// Resolve the module path to actual files (cached)
		cacheKey := effectiveModulePath + "|" + imp.File
		targetFiles, cached := moduleCache[cacheKey]
		if !cached {
			targetFiles = resolveModulePath(effectiveModulePath, fileMap)
			moduleCache[cacheKey] = targetFiles
		}

		// If module path didn't resolve, try progressively shorter suffixes.
		// This handles Go ("github.com/gin-gonic/gin/render" → "render"),
		// Rust ("crate::foo::bar" → "foo::bar" → "bar"),
		// and any language where import paths have a repo/org prefix.
		if len(targetFiles) == 0 && imp.ModulePath != "" {
			// Try suffix variants of the module path
			slashPath := strings.ReplaceAll(imp.ModulePath, "::", "/")
			slashPath = strings.ReplaceAll(slashPath, ".", "/")
			slashPath = strings.ReplaceAll(slashPath, `\`, "/")
			parts := strings.Split(slashPath, "/")
			for j := 1; j < len(parts); j++ {
				suffix := strings.Join(parts[j:], "/")
				cacheKey := "suffix:" + suffix
				if cached, ok := moduleCache[cacheKey]; ok {
					targetFiles = cached
					break
				}
				found := resolveModulePath(suffix, fileMap)
				moduleCache[cacheKey] = found
				if len(found) > 0 {
					targetFiles = found
					break
				}
				// Also try dot-joined suffix (Python-style)
				dotSuffix := strings.Join(parts[j:], ".")
				if dotSuffix != suffix {
					cacheKey2 := "suffix:" + dotSuffix
					if cached, ok := moduleCache[cacheKey2]; ok {
						if len(cached) > 0 {
							targetFiles = cached
							break
						}
						continue
					}
					found2 := resolveModulePath(dotSuffix, fileMap)
					moduleCache[cacheKey2] = found2
					if len(found2) > 0 {
						targetFiles = found2
						break
					}
				}
			}
		}

		// Also try module_path + imported_name combined (for Python "from X import Y"
		// where Y is a submodule, not a symbol)
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

// resolvePythonRelativeImport converts a dot-prefixed relative import path
// to an absolute module path using the importing file's location.
//
// Examples:
//   - ".foo" in "pkg/sub/mod.py" → "pkg.sub.foo"
//   - "..utils" in "pkg/sub/mod.py" → "pkg.utils"
//   - "." in "pkg/mod.py" (from . import x) → "pkg" (then x appended by caller)
//   - "...core" in "pkg/sub/deep/mod.py" → "pkg.core"
func resolvePythonRelativeImport(modulePath string, importingFile string) string {
	// Count leading dots
	dotCount := 0
	for _, ch := range modulePath {
		if ch == '.' {
			dotCount++
		} else {
			break
		}
	}
	remainder := modulePath[dotCount:] // e.g., "foo", "utils", "" (for "from . import")

	// Convert importing file to package path components
	// "pkg/sub/mod.py" → ["pkg", "sub"]  (drop filename, keep dirs)
	normalized := filepath.ToSlash(importingFile)
	dir := filepath.ToSlash(filepath.Dir(normalized))
	if dir == "." || dir == "" {
		// File is at root level — relative imports don't work here
		if remainder != "" {
			return remainder
		}
		return modulePath
	}

	parts := strings.Split(dir, "/")

	// Go up (dotCount - 1) levels. One dot = current package, two dots = parent, etc.
	levelsUp := dotCount - 1
	if levelsUp > len(parts) {
		levelsUp = len(parts)
	}
	if levelsUp > 0 {
		parts = parts[:len(parts)-levelsUp]
	}

	// Build absolute module path
	basePath := strings.Join(parts, ".")
	if remainder != "" {
		if basePath != "" {
			return basePath + "." + remainder
		}
		return remainder
	}
	return basePath
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

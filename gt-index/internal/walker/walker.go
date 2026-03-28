// Package walker discovers source files in a directory tree.
package walker

import (
	"bufio"
	"os"
	"path/filepath"
	"strings"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
)

// skipDirs are directories to always skip.
var skipDirs = map[string]bool{
	".git":          true,
	"__pycache__":   true,
	"node_modules":  true,
	".tox":          true,
	".eggs":         true,
	".venv":         true,
	"venv":          true,
	".mypy_cache":   true,
	"dist":          true,
	"build":         true,
	".next":         true,
	"vendor":        true, // Go vendor — can optionally include
	"target":        true, // Rust/Java build output
}

// SourceFile represents a discovered source file.
type SourceFile struct {
	Path     string // relative path from root
	AbsPath  string
	Language string
	Spec     *specs.Spec
}

// Walk discovers all source files under root that have a registered language spec.
// Respects .gitignore patterns (basic implementation).
func Walk(root string, maxFiles int) ([]SourceFile, error) {
	root, _ = filepath.Abs(root)

	// Load .gitignore patterns
	ignorePatterns := loadGitignore(filepath.Join(root, ".gitignore"))

	var files []SourceFile
	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return nil // skip errors
		}
		if len(files) >= maxFiles {
			return filepath.SkipAll
		}

		// Skip hidden and known directories
		if info.IsDir() {
			base := filepath.Base(path)
			if skipDirs[base] || strings.HasPrefix(base, ".") {
				return filepath.SkipDir
			}
			return nil
		}

		// Skip large files (>500KB)
		if info.Size() > 500*1024 {
			return nil
		}

		// Check if file has a registered language spec
		ext := filepath.Ext(path)
		spec := specs.ForExtension(ext)
		if spec == nil {
			return nil
		}

		relPath, _ := filepath.Rel(root, path)
		relPath = filepath.ToSlash(relPath)

		// Check gitignore
		if isIgnored(relPath, ignorePatterns) {
			return nil
		}

		files = append(files, SourceFile{
			Path:     relPath,
			AbsPath:  path,
			Language: spec.Name,
			Spec:     spec,
		})
		return nil
	})

	return files, err
}

func loadGitignore(path string) []string {
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()

	var patterns []string
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		patterns = append(patterns, line)
	}
	return patterns
}

func isIgnored(relPath string, patterns []string) bool {
	for _, p := range patterns {
		// Simple glob matching
		if matched, _ := filepath.Match(p, filepath.Base(relPath)); matched {
			return true
		}
		if strings.Contains(relPath, p) {
			return true
		}
	}
	return false
}

// IsTestFile checks if a file path is a test file based on conventions.
func IsTestFile(relPath string) bool {
	base := filepath.Base(relPath)
	dir := filepath.Dir(relPath)

	// Python: test_*.py, *_test.py
	if strings.HasPrefix(base, "test_") || strings.HasSuffix(strings.TrimSuffix(base, filepath.Ext(base)), "_test") {
		return true
	}
	// Go: *_test.go
	if strings.HasSuffix(base, "_test.go") {
		return true
	}
	// JS/TS: *.test.js, *.spec.js, *.test.ts, *.spec.ts
	if strings.Contains(base, ".test.") || strings.Contains(base, ".spec.") {
		return true
	}
	// In tests/ or __tests__/ directory
	if strings.Contains(dir, "tests") || strings.Contains(dir, "__tests__") || strings.Contains(dir, "test/") {
		return true
	}
	return false
}

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
	base := filepath.Base(relPath)
	for _, p := range patterns {
		// Glob matching against basename
		if matched, _ := filepath.Match(p, base); matched {
			return true
		}
		// Directory-level matching: pattern matches a directory component
		// e.g. "vendor" matches "vendor/foo.go" but NOT "foo_vendor.go"
		// e.g. "_test" matches "_test/foo.go" but NOT "foo_test.go"
		if strings.Contains(p, "/") {
			// Path pattern: match against full relative path
			if matched, _ := filepath.Match(p, relPath); matched {
				return true
			}
		} else {
			// Simple name: match as directory component only
			dirPart := "/" + filepath.ToSlash(relPath) + "/"
			if strings.Contains(dirPart, "/"+p+"/") {
				return true
			}
		}
	}
	return false
}

// IsTestFile checks if a file path is a test file based on conventions.
func IsTestFile(relPath string) bool {
	base := filepath.Base(relPath)
	dir := filepath.ToSlash(filepath.Dir(relPath))
	ext := filepath.Ext(base)
	stem := strings.TrimSuffix(base, ext)

	// Python: test_*.py, *_test.py
	if strings.HasPrefix(base, "test_") || strings.HasSuffix(stem, "_test") {
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
	// JVM (Java/Kotlin/Scala/Groovy): *Test.java, *Tests.java, *Test.kt, etc.
	if strings.HasSuffix(stem, "Test") || strings.HasSuffix(stem, "Tests") || strings.HasSuffix(stem, "Spec") {
		switch ext {
		case ".java", ".kt", ".kts", ".scala", ".groovy":
			return true
		}
	}
	// C#: *Test.cs, *Tests.cs
	if (strings.HasSuffix(stem, "Test") || strings.HasSuffix(stem, "Tests")) && ext == ".cs" {
		return true
	}
	// PHP: *Test.php (PHPUnit convention)
	if strings.HasSuffix(stem, "Test") && ext == ".php" {
		return true
	}
	// Swift: *Tests.swift
	if (strings.HasSuffix(stem, "Tests") || strings.HasSuffix(stem, "Test")) && ext == ".swift" {
		return true
	}
	// Ruby: *_spec.rb (RSpec convention)
	if strings.HasSuffix(stem, "_spec") && ext == ".rb" {
		return true
	}
	// Directory-based: tests/, __tests__/, test/, spec/ (all languages)
	if strings.Contains(dir, "tests") || strings.Contains(dir, "__tests__") || strings.Contains(dir, "test/") || strings.Contains(dir, "spec/") {
		return true
	}
	// JVM convention: src/test/ directory
	if strings.Contains(dir, "src/test/") {
		return true
	}
	return false
}

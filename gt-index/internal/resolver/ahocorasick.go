// Package resolver — Aho-Corasick enhanced name resolution.
//
// For languages WITHOUT import extractors, this provides better-than-fallback
// cross-file resolution by using exact name matching with file proximity heuristics.
package resolver

import (
	"path/filepath"
	"strings"

	"github.com/cloudflare/ahocorasick"
)

// ACIndex wraps an Aho-Corasick automaton for fast multi-pattern name matching.
type ACIndex struct {
	matcher     *ahocorasick.Matcher
	patterns    []string            // pattern strings, parallel to matcher indices
	nameToIDs   map[string][]int64  // name → list of node IDs
	idToFile    map[int64]string    // node ID → file path
}

// NewACIndex builds an Aho-Corasick automaton from all symbol names in the index.
// idToFile maps each node DB ID to its source file path (for proximity scoring).
func NewACIndex(nameIndex map[string][]int64, nodeFiles map[int64]string) *ACIndex {
	patterns := make([]string, 0, len(nameIndex))
	for name := range nameIndex {
		patterns = append(patterns, name)
	}

	m := ahocorasick.NewStringMatcher(patterns)
	return &ACIndex{
		matcher:   m,
		patterns:  patterns,
		nameToIDs: nameIndex,
		idToFile:  nodeFiles,
	}
}

// ResolveByProximity resolves a callee name to the best-matching node ID,
// preferring definitions in files close to the caller.
//
// Returns (targetNodeID, found). If ambiguous with no proximity signal, picks the first.
func (ac *ACIndex) ResolveByProximity(calleeName string, callerFile string, callerNodeID int64) (int64, bool) {
	candidates, ok := ac.nameToIDs[calleeName]
	if !ok || len(candidates) == 0 {
		return 0, false
	}

	// Filter out self-references
	var valid []int64
	for _, id := range candidates {
		if id != callerNodeID {
			valid = append(valid, id)
		}
	}
	if len(valid) == 0 {
		return 0, false
	}

	// If unique match, return it
	if len(valid) == 1 {
		return valid[0], true
	}

	// Multiple candidates — use proximity heuristic
	callerDir := filepath.Dir(callerFile)
	callerParts := strings.Split(filepath.ToSlash(callerDir), "/")

	bestID := valid[0]
	bestScore := -1

	for _, id := range valid {
		targetFile, ok := ac.idToFile[id]
		if !ok {
			continue
		}

		targetDir := filepath.Dir(targetFile)
		targetParts := strings.Split(filepath.ToSlash(targetDir), "/")

		// Score: count shared path prefix components
		score := 0
		minLen := len(callerParts)
		if len(targetParts) < minLen {
			minLen = len(targetParts)
		}
		for i := 0; i < minLen; i++ {
			if callerParts[i] == targetParts[i] {
				score++
			} else {
				break
			}
		}

		// Same directory gets a big bonus
		if callerDir == targetDir {
			score += 100
		}

		if score > bestScore {
			bestScore = score
			bestID = id
		}
	}

	return bestID, true
}

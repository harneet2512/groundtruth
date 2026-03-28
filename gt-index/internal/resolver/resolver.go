// Package resolver resolves call references to definition nodes.
package resolver

import (
	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

// ResolvedCall is a call reference that has been resolved to a target node.
type ResolvedCall struct {
	SourceNodeID int64
	TargetNodeID int64
	SourceLine   int
	SourceFile   string
	Method       string // "name_match", "aho_corasick"
}

// Resolve takes all call refs and all defined nodes, and resolves calls to definitions.
func Resolve(
	allCalls []parser.CallRef,
	nodeIDs map[string][]int64, // name → list of node IDs
	fileNodeIDs map[string]map[string]int64, // file → name → node ID (for same-file preference)
	callerNodeIDs []int64, // parallel to allCalls: the DB ID of each caller node
) []ResolvedCall {
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

		// Strategy 2: Cross-file name match
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

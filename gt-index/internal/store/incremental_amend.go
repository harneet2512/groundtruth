package store

// incremental_amend.go — per-file (-file) amend helpers that keep the rows a
// reparsed or deleted file's neighbours point at consistent with the new node
// ids. Found by the incremental-vs-clean convergence harness
// (cmd/gt-index/convergence_test.go).

import (
	"database/sql"
	"fmt"
)

// TaxonomyEdgeTypes are the edge kinds internal/taxonomy derives (always with
// evidence_type 'syntax'). The per-file amend re-derives them whole-graph, so
// its incoming-edge snapshot must not restore them as name-matched guesses.
var TaxonomyEdgeTypes = []string{
	"DECLARED_IMPLEMENTS", "OVERRIDES", "METHOD_OVERRIDES", "DECORATES",
	"PARAM_TYPE", "INJECTS", "ACCESSES", "RETURNS_TYPE",
}

// DeleteFileHashTx removes a deleted file's content hash, so a later -file on
// a recreated path is not short-circuited against the old bytes.
func DeleteFileHashTx(tx *sql.Tx, filePath string) error {
	if _, err := tx.Exec(`DELETE FROM file_hashes WHERE file_path = ?`, filePath); err != nil {
		return fmt.Errorf("delete file hash for %s: %w", filePath, err)
	}
	return nil
}

// IncomingAssertionRef is one assertion in ANOTHER file whose resolved target
// is a node of the reparsed file. assertions.target_node_id has no foreign
// key; without a rebind the node delete leaves it naming a row that no longer
// exists (a dangling id a reader would dereference into nothing — or, once the
// AUTOINCREMENT id is reused, into an unrelated symbol).
type IncomingAssertionRef struct {
	AssertionID      int64
	OldLabel         string
	OldQualifiedName string
	OldName          string
}

// SnapshotIncomingAssertionsTx captures the cross-file assertions targeting
// filePath's nodes, before those nodes are deleted.
func SnapshotIncomingAssertionsTx(tx *sql.Tx, filePath string) ([]IncomingAssertionRef, error) {
	rows, err := tx.Query(
		`SELECT a.id, n.label, COALESCE(n.qualified_name, ''), n.name
		   FROM assertions a
		   JOIN nodes n ON n.id = a.target_node_id
		   JOIN nodes t ON t.id = a.test_node_id
		  WHERE n.file_path = ? AND t.file_path != ?
		  ORDER BY a.id`, filePath, filePath)
	if err != nil {
		return nil, fmt.Errorf("snapshot incoming assertions for %s: %w", filePath, err)
	}
	defer rows.Close()
	var out []IncomingAssertionRef
	for rows.Next() {
		var r IncomingAssertionRef
		if err := rows.Scan(&r.AssertionID, &r.OldLabel, &r.OldQualifiedName, &r.OldName); err != nil {
			return nil, fmt.Errorf("scan incoming assertion: %w", err)
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// RebindIncomingAssertionsTx points each snapshotted assertion at the new node
// with the same (label, qualified_name) — (label, name) when unqualified —
// when exactly one exists, and otherwise clears the target to 0 (unresolved).
// It never leaves a target naming a deleted row. Returns (rebound, cleared).
func RebindIncomingAssertionsTx(tx *sql.Tx, snap []IncomingAssertionRef, newNodes []*Node, newIDs []int64) (int, int, error) {
	if len(snap) == 0 {
		return 0, 0, nil
	}
	byQualified := make(map[string][]int64)
	byName := make(map[string][]int64)
	for i, n := range newNodes {
		if n == nil || i >= len(newIDs) || newIDs[i] <= 0 {
			continue
		}
		if n.QualifiedName != "" {
			byQualified[n.Label+"\x00"+n.QualifiedName] = append(byQualified[n.Label+"\x00"+n.QualifiedName], newIDs[i])
		}
		byName[n.Label+"\x00"+n.Name] = append(byName[n.Label+"\x00"+n.Name], newIDs[i])
	}
	update, err := tx.Prepare(`UPDATE assertions SET target_node_id = ? WHERE id = ?`)
	if err != nil {
		return 0, 0, fmt.Errorf("prepare assertion rebind: %w", err)
	}
	defer update.Close()
	rebound, cleared := 0, 0
	for _, r := range snap {
		var matches []int64
		if r.OldQualifiedName != "" {
			matches = byQualified[r.OldLabel+"\x00"+r.OldQualifiedName]
		} else {
			matches = byName[r.OldLabel+"\x00"+r.OldName]
		}
		target := int64(0)
		if len(matches) == 1 {
			target = matches[0]
			rebound++
		} else {
			cleared++
		}
		if _, err := update.Exec(target, r.AssertionID); err != nil {
			return rebound, cleared, fmt.Errorf("rebind assertion %d: %w", r.AssertionID, err)
		}
	}
	return rebound, cleared, nil
}

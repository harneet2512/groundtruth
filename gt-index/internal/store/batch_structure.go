package store

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"reflect"
)

// ReplaceParsedStructure keeps parser-owned rows whose entire source file and
// declaration are unchanged. All repository-wide products are recomputed by
// the normal pipeline. The caller operates only on a private staged database.
//
// preserveCoupling asks the amend to keep the copied parent's cochanges,
// communities and community_members rows instead of clearing them with the
// rest of the repository-wide products. The caller sets it only after proving
// the amend re-walks the parent's recorded history window — see
// resolveCouplingReuse in cmd/gt-index/derived.go — and the publication path
// still decides per table whether the carried rows stand or are rebuilt.
func (d *DB) ReplaceParsedStructure(nodes []*Node, amend bool, executableSHA string, preserveCoupling bool) ([]int64, int, error) {
	tx, err := d.db.Begin()
	if err != nil {
		return nil, 0, err
	}
	defer tx.Rollback()
	if amend {
		var payload, digest string
		if err := tx.QueryRow(`SELECT value FROM project_meta WHERE key=?`, CorePhaseReceiptKey).Scan(&payload); err != nil {
			return nil, 0, err
		}
		if err := tx.QueryRow(`SELECT value FROM project_meta WHERE key=?`, CorePhaseReceiptSHA256Key).Scan(&digest); err != nil {
			return nil, 0, err
		}
		var receipt CorePhaseReceipt
		if err := json.Unmarshal([]byte(payload), &receipt); err != nil {
			return nil, 0, err
		}
		sealed, actual, err := receipt.Seal()
		if err != nil || sealed != payload || actual != digest || executableSHA == "" || receipt.ExecutableSHA256 != executableSHA {
			return nil, 0, fmt.Errorf("batch parent core identity does not match producer")
		}
		var inventory int
		if err := tx.QueryRow(`SELECT count(*) FROM sqlite_master WHERE type='table' AND name='parser_node_inventory'`).Scan(&inventory); err != nil {
			return nil, 0, err
		}
		if inventory != 1 {
			return nil, 0, fmt.Errorf("batch parent has no parser inventory; full build required")
		}
	}
	if _, err := tx.Exec(`CREATE TABLE IF NOT EXISTS parser_node_inventory (
		identity TEXT PRIMARY KEY, node_id INTEGER NOT NULL UNIQUE REFERENCES nodes(id))`); err != nil {
		return nil, 0, err
	}
	old := make(map[string]int64)
	rows, err := tx.Query(`SELECT identity,node_id FROM parser_node_inventory`)
	if err != nil {
		return nil, 0, err
	}
	for rows.Next() {
		var key string
		var id int64
		if err := rows.Scan(&key, &id); err != nil {
			rows.Close()
			return nil, 0, err
		}
		old[key] = id
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return nil, 0, err
	}
	rows.Close()
	if err := ensureParsedFactInventoryTx(tx); err != nil {
		return nil, 0, err
	}
	// Parent pointers are resolved from this revision's complete parser inputs
	// after this transaction. Never carry an old resolution or analysis receipt.
	if amend {
		tables := []string{"resolution_candidates", "resolution_callsites", "resolution_symbols", "closure", "community_members", "communities", "process_steps", "processes", "cochanges", "file_hashes", "project_meta"}
		if preserveCoupling {
			// The caller proved the amend re-walks the parent's recorded
			// coupling window; the copied tables stay for the derived-layers
			// pass to adopt or rebuild rather than being cleared here.
			kept := tables[:0]
			for _, table := range tables {
				if table == "community_members" || table == "communities" || table == "cochanges" {
					continue
				}
				kept = append(kept, table)
			}
			tables = kept
		}
		for _, table := range tables {
			var exists int
			if err := tx.QueryRow(`SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?`, table).Scan(&exists); err != nil {
				return nil, 0, err
			}
			if exists != 0 {
				if _, err := tx.Exec(`DELETE FROM "` + table + `"`); err != nil {
					return nil, 0, fmt.Errorf("clear %s: %w", table, err)
				}
			}
		}
		if _, err := tx.Exec(`UPDATE nodes SET parent_id=NULL`); err != nil {
			return nil, 0, err
		}
	}
	if _, err := tx.Exec(`DELETE FROM parser_node_inventory`); err != nil {
		return nil, 0, err
	}
	ids := make([]int64, len(nodes))
	keys := make([]string, len(nodes))
	ordinals := make(map[string]int)
	retained := 0
	for i, node := range nodes {
		// Parent indexes have already been zeroed; file-local ordinal distinguishes
		// identical declarations. FileHash invalidates every node in an edited file.
		material, err := json.Marshal(struct {
			Node    *Node
			Ordinal int
		}{node, ordinals[node.FilePath]})
		if err != nil {
			return nil, 0, err
		}
		ordinals[node.FilePath]++
		sum := sha256.Sum256(material)
		keys[i] = hex.EncodeToString(sum[:])
		if node.FileHash != "" {
			ids[i] = old[keys[i]]
		}
		if ids[i] != 0 {
			var actual Node
			err := tx.QueryRow(`SELECT label,name,COALESCE(qualified_name,''),file_path,
				COALESCE(start_line,0),COALESCE(end_line,0),COALESCE(signature,''),COALESCE(return_type,''),
				is_exported,is_test,language,COALESCE(file_hash,''),COALESCE(byte_start,0),COALESCE(byte_end,0)
				FROM nodes WHERE id=?`, ids[i]).Scan(&actual.Label, &actual.Name, &actual.QualifiedName, &actual.FilePath,
				&actual.StartLine, &actual.EndLine, &actual.Signature, &actual.ReturnType, &actual.IsExported, &actual.IsTest,
				&actual.Language, &actual.FileHash, &actual.ByteStart, &actual.ByteEnd)
			if err != nil || !reflect.DeepEqual(actual, *node) {
				return nil, 0, fmt.Errorf("batch parent parser row differs from its inventory: %s", node.FilePath)
			}
			retained++
			if _, err := tx.Exec(`INSERT INTO parser_node_inventory(identity,node_id) VALUES(?,?)`, keys[i], ids[i]); err != nil {
				return nil, 0, err
			}
		}
	}
	if amend {
		// Only parser-owned facts on retained nodes survive into pass 4. That
		// pass rechecks all values, including every assertion's new target.
		for _, statement := range []string{
			`DELETE FROM parser_edge_inventory WHERE edge_id IN (SELECT id FROM edges WHERE source_id NOT IN (SELECT node_id FROM parser_node_inventory) OR target_id NOT IN (SELECT node_id FROM parser_node_inventory))`,
			`DELETE FROM edges WHERE id NOT IN (SELECT edge_id FROM parser_edge_inventory)`,
			`DELETE FROM parser_property_inventory WHERE property_id IN (SELECT id FROM properties WHERE node_id NOT IN (SELECT node_id FROM parser_node_inventory))`,
			`DELETE FROM properties WHERE id NOT IN (SELECT property_id FROM parser_property_inventory)`,
			`DELETE FROM parser_assertion_inventory WHERE assertion_id IN (SELECT id FROM assertions WHERE test_node_id NOT IN (SELECT node_id FROM parser_node_inventory))`,
			`DELETE FROM assertions WHERE id NOT IN (SELECT assertion_id FROM parser_assertion_inventory)`,
		} {
			if _, err := tx.Exec(statement); err != nil {
				return nil, 0, err
			}
		}
		if _, err := tx.Exec(`DELETE FROM nodes WHERE id NOT IN (SELECT node_id FROM parser_node_inventory)`); err != nil {
			return nil, 0, err
		}
	}
	var missing []*Node
	var positions []int
	for i, node := range nodes {
		if ids[i] == 0 {
			missing = append(missing, node)
			positions = append(positions, i)
		}
	}
	inserted, err := BatchInsertNodesTx(tx, missing)
	if err != nil {
		return nil, 0, err
	}
	for j, id := range inserted {
		i := positions[j]
		ids[i] = id
		if _, err := tx.Exec(`INSERT INTO parser_node_inventory(identity,node_id) VALUES(?,?)`, keys[i], id); err != nil {
			return nil, 0, err
		}
	}
	if err := tx.Commit(); err != nil {
		return nil, 0, err
	}
	return ids, retained, nil
}

package store

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
)

func ensureParsedFactInventoryTx(tx *sql.Tx) error {
	for _, ddl := range []string{
		`CREATE TABLE IF NOT EXISTS parser_property_inventory (property_id INTEGER PRIMARY KEY REFERENCES properties(id), content_sha256 TEXT NOT NULL)`,
		`CREATE TABLE IF NOT EXISTS parser_assertion_inventory (assertion_id INTEGER PRIMARY KEY REFERENCES assertions(id), content_sha256 TEXT NOT NULL)`,
		`CREATE TABLE IF NOT EXISTS parser_edge_inventory (edge_id INTEGER PRIMARY KEY REFERENCES edges(id), content_sha256 TEXT NOT NULL)`,
	} {
		if _, err := tx.Exec(ddl); err != nil {
			return err
		}
	}
	return nil
}

// Only these syntax-derived kinds belong to this owner. Resolution and analysis
// edges remain owned by their existing publication transactions.
func parsedEdgeKinds() []string {
	return append([]string{"CONTAINS"}, specs.AllTaxonomyEdgeKinds...)
}

func (d *DB) ReconcileParsedEdges(edges []*Edge) (int, error) {
	values := make([][]any, 0, len(edges))
	for _, e := range edges {
		allowed := false
		for _, kind := range parsedEdgeKinds() {
			allowed = allowed || e.Type == kind
		}
		if !allowed {
			return 0, fmt.Errorf("non-parser edge kind: %s", e.Type)
		}
		values = append(values, []any{e.SourceID, e.TargetID, e.Type, e.SourceLine, e.SourceFile,
			e.ResolutionMethod, e.Confidence, e.Metadata, e.TrustTier, e.CandidateCount, e.EvidenceType, e.VerificationStatus})
	}
	return d.reconcileParsedFacts("edges", "parser_edge_inventory", "edge_id",
		[]string{"source_id", "target_id", "type", "source_line", "source_file", "resolution_method", "confidence", "metadata", "trust_tier", "candidate_count", "evidence_type", "verification_status"}, values)
}

// ReconcileParsedProperties retains exact parser-owned facts. Derived properties
// are not in this inventory and continue to be rebuilt by their existing owner.
func (d *DB) ReconcileParsedProperties(props []*Property) (int, error) {
	values := make([][]any, 0, len(props))
	for _, p := range props {
		values = append(values, []any{p.NodeID, p.Kind, p.Value, p.Line, p.Confidence})
	}
	return d.reconcileParsedFacts("properties", "parser_property_inventory", "property_id",
		[]string{"node_id", "kind", "value", "line", "confidence"}, values)
}

// Assertions include the freshly resolved target and score in their identity.
// Unchanged test source alone cannot preserve an obsolete target resolution.
func (d *DB) ReconcileParsedAssertions(assertions []*Assertion) (int, error) {
	values := make([][]any, 0, len(assertions))
	for _, a := range assertions {
		values = append(values, []any{a.TestNodeID, a.TargetNodeID, a.ResolutionScore,
			a.Kind, a.Expression, a.Expected, a.Line})
	}
	return d.reconcileParsedFacts("assertions", "parser_assertion_inventory", "assertion_id",
		[]string{"test_node_id", "target_node_id", "resolution_score", "kind", "expression", "expected", "line"}, values)
}

func factValuesDigest(values []any) (string, error) {
	material, err := json.Marshal(values)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(material)
	return hex.EncodeToString(digest[:]), nil
}

// SQL identifiers here are private constants supplied by the two typed owners
// above, not repository text. The whole replacement and its inventory commit
// together; an invalid parent fact leaves the staged transaction unchanged.
func (d *DB) reconcileParsedFacts(table, inventory, idColumn string, columns []string, desired [][]any) (int, error) {
	tx, err := d.db.Begin()
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	if err := ensureParsedFactInventoryTx(tx); err != nil {
		return 0, err
	}
	selected := make([]string, len(columns))
	for i, column := range columns {
		selected[i] = "p." + column
	}
	rows, err := tx.Query("SELECT p.id,i.content_sha256," + strings.Join(selected, ",") +
		" FROM " + table + " p JOIN " + inventory + " i ON i." + idColumn + "=p.id ORDER BY p.id")
	if err != nil {
		return 0, err
	}
	old := make(map[string][]int64)
	for rows.Next() {
		var id int64
		var expected string
		values := make([]any, len(columns))
		targets := []any{&id, &expected}
		for i := range values {
			targets = append(targets, &values[i])
		}
		if err := rows.Scan(targets...); err != nil {
			rows.Close()
			return 0, err
		}
		actual, err := factValuesDigest(values)
		if err != nil || actual != expected {
			rows.Close()
			return 0, fmt.Errorf("batch parent %s row differs from parser inventory: %d", table, id)
		}
		old[actual] = append(old[actual], id)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return 0, err
	}
	rows.Close()
	if _, err := tx.Exec("DELETE FROM " + inventory); err != nil {
		return 0, err
	}
	placeholders := strings.TrimSuffix(strings.Repeat("?,", len(columns)), ",")
	insert, err := tx.Prepare("INSERT INTO " + table + " (" + strings.Join(columns, ",") + ") VALUES(" + placeholders + ")")
	if err != nil {
		return 0, err
	}
	defer insert.Close()
	mark, err := tx.Prepare("INSERT INTO " + inventory + "(" + idColumn + ",content_sha256) VALUES(?,?)")
	if err != nil {
		return 0, err
	}
	defer mark.Close()
	retained := 0
	for _, values := range desired {
		digest, err := factValuesDigest(values)
		if err != nil {
			return 0, err
		}
		var id int64
		if available := old[digest]; len(available) > 0 {
			id = available[0]
			old[digest] = available[1:]
			retained++
		} else {
			result, err := insert.Exec(values...)
			if err != nil {
				return 0, err
			}
			id, err = result.LastInsertId()
			if err != nil {
				return 0, err
			}
		}
		if _, err := mark.Exec(id, digest); err != nil {
			return 0, err
		}
	}
	scope := ""
	var scopeArgs []any
	if table == "edges" {
		kinds := parsedEdgeKinds()
		scope = "type IN (" + strings.TrimSuffix(strings.Repeat("?,", len(kinds)), ",") + ") AND "
		for _, kind := range kinds {
			scopeArgs = append(scopeArgs, kind)
		}
	}
	if _, err := tx.Exec("DELETE FROM "+table+" WHERE "+scope+"id NOT IN (SELECT "+idColumn+" FROM "+inventory+")", scopeArgs...); err != nil {
		return 0, err
	}
	if table == "properties" {
		if err := PopulatePropertiesFTS5Tx(tx); err != nil {
			return 0, err
		}
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return retained, nil
}

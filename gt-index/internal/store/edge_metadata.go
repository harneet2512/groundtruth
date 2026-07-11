package store

// edge_metadata.go — B-24: normalize the polymorphic edges.metadata field into ONE
// canonical, schema-versioned, individually-queryable surface.
//
// edges.metadata is written in two incompatible shapes:
//   - API_CALL edges  → JSON object, e.g. {"route":"/orders","method":"GET"} (api_edges.go)
//   - promoted CALLS  → semicolon-separated key=value, e.g.
//                       receiver_type=UserRepo;dataflow=fetch;usage=return
//                       (main.go receiverEdgeMetadata + promote.go dataflow/usage appends)
// Consumers therefore reach for regex / SQL LIKE '%dataflow=%' and each rolls its own
// parser. ParseEdgeMetadata is the ONE canonical parser for BOTH shapes; PopulateEdgeMetadata
// materialises it into the edge_metadata(edge_id,key,value,schema_version) sub-table so a
// field is a real indexed query: SELECT value FROM edge_metadata WHERE edge_id=? AND key=?.
//
// This is ADDITIVE: edges.metadata itself is left byte-identical, so the receiver-provenance
// cleanliness invariant (main.go receiverEdgeMetadata) and the incremental restore logic
// (incremental.go stripReceiverTypeTag) are untouched. edge_metadata is a derived cache of
// edges.metadata (which the revision already hashes), so it is intentionally NOT hashed
// again by the composite revision. Absent on an old graph.db → consumers fall back to
// ParseEdgeMetadata on the raw string (correct-or-quiet).

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
)

// EdgeMetadataSchemaVersion is stamped on every normalized row so the format is
// self-describing. Bump when the canonical key set / encoding changes.
const EdgeMetadataSchemaVersion = 1

// ParseEdgeMetadata parses a raw edges.metadata value (JSON object OR semicolon-separated
// key=value) into a normalized key→value map. Deterministic and total: an empty / malformed
// input yields an empty map (correct-or-quiet), never an error. A JSON scalar value is
// stringified (numbers/bools become their literal text) so every field is a string.
func ParseEdgeMetadata(raw string) map[string]string {
	out := map[string]string{}
	s := strings.TrimSpace(raw)
	if s == "" {
		return out
	}
	if strings.HasPrefix(s, "{") {
		var obj map[string]any
		if err := json.Unmarshal([]byte(s), &obj); err == nil {
			for k, v := range obj {
				k = strings.TrimSpace(k)
				if k == "" {
					continue
				}
				out[k] = jsonScalarString(v)
			}
			return out
		}
		// Malformed JSON → fall through to key=value parsing (correct-or-quiet).
	}
	for _, seg := range strings.Split(s, ";") {
		seg = strings.TrimSpace(seg)
		if seg == "" {
			continue
		}
		eq := strings.IndexByte(seg, '=')
		if eq <= 0 {
			continue // no key, or empty key — skip, never fabricate a field
		}
		key := strings.TrimSpace(seg[:eq])
		val := seg[eq+1:]
		if key == "" {
			continue
		}
		out[key] = val
	}
	return out
}

// jsonScalarString renders a decoded JSON scalar as a stable string. Non-scalars
// (arrays/objects) are re-marshalled so the value is still queryable, never dropped.
//
// Graph-F10 (KNOWN, INFO-level divergence, bounce 2026-07-10): encoding/json decodes every
// JSON number into a float64, so an INTEGER beyond float64's 2^53 exact-integer mantissa
// loses precision here (and a value > 2^63 makes the int64(t) conversion implementation-
// defined). The Python twin (curation_map._json_scalar_str) keeps arbitrary precision via
// str(int), so the two parsers would DISAGREE on such a value. This does NOT bite: every
// real edges.metadata key (receiver_type / route / method / dataflow / usage) is a STRING,
// and the `;`-separated key=value form (the CALLS path) never numeric-coerces at all. The
// boundary is pinned by TestParseEdgeMetadataScalarParity so a regression — or a future
// numeric key — is caught rather than silently diverging.
func jsonScalarString(v any) string {
	switch t := v.(type) {
	case string:
		return t
	case bool:
		if t {
			return "true"
		}
		return "false"
	case float64:
		// Integers render without a trailing ".0"; fractional keep full precision.
		if t == float64(int64(t)) {
			return fmt.Sprintf("%d", int64(t))
		}
		return fmt.Sprintf("%g", t)
	case nil:
		return ""
	default:
		b, _ := json.Marshal(t)
		return string(b)
	}
}

// PopulateEdgeMetadata (re)builds the edge_metadata sub-table from the current edges.metadata
// values via ParseEdgeMetadata. Idempotent (clears first) and deterministic (edges in id
// order, keys sorted). Non-fatal / back-compat: if the edge_metadata table is absent (old
// graph.db opened read-only), it returns nil.
func (d *DB) PopulateEdgeMetadata() error {
	var n int
	if err := d.db.QueryRow(
		"SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='edge_metadata'",
	).Scan(&n); err != nil || n == 0 {
		return nil
	}
	if _, err := d.db.Exec("DELETE FROM edge_metadata"); err != nil {
		return fmt.Errorf("clear edge_metadata: %w", err)
	}
	rows, err := d.db.Query(
		`SELECT id, COALESCE(metadata,'') FROM edges
		  WHERE metadata IS NOT NULL AND metadata != '' ORDER BY id`,
	)
	if err != nil {
		return fmt.Errorf("scan edges for edge_metadata: %w", err)
	}
	type pair struct {
		edgeID int64
		meta   string
	}
	var pending []pair
	for rows.Next() {
		var p pair
		if err := rows.Scan(&p.edgeID, &p.meta); err != nil {
			rows.Close()
			return fmt.Errorf("scan edge metadata row: %w", err)
		}
		pending = append(pending, p)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}

	tx, err := d.db.Begin()
	if err != nil {
		return fmt.Errorf("begin edge_metadata populate: %w", err)
	}
	stmt, err := tx.Prepare(
		"INSERT OR REPLACE INTO edge_metadata(edge_id, key, value, schema_version) VALUES (?, ?, ?, ?)",
	)
	if err != nil {
		tx.Rollback()
		return fmt.Errorf("prepare edge_metadata insert: %w", err)
	}
	defer stmt.Close()
	for _, p := range pending {
		kv := ParseEdgeMetadata(p.meta)
		keys := make([]string, 0, len(kv))
		for k := range kv {
			keys = append(keys, k)
		}
		sort.Strings(keys) // deterministic insert order
		for _, k := range keys {
			if _, err := stmt.Exec(p.edgeID, k, kv[k], EdgeMetadataSchemaVersion); err != nil {
				tx.Rollback()
				return fmt.Errorf("insert edge_metadata for edge %d key %q: %w", p.edgeID, k, err)
			}
		}
	}
	return tx.Commit()
}

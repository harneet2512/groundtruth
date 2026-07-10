// Package store: deterministic logical-content revision hash.
//
// post_revision is the content fingerprint the incremental (`-file`) executor
// contract returns on stdout so a consuming overlay (the Python side) can tell
// whether graph.db's LOGICAL graph changed WITHOUT re-scanning it, and can key a
// cache on the exact graph state. Two invariants:
//
//   - DETERMINISM: two identical `-file` runs (same starting db, same file) ->
//     byte-identical post_revision. Rows are canonicalised to length-framed
//     records and SORTED before hashing, so SQLite scan order never leaks in.
//   - SENSITIVITY: any change to a hashed column -> a different post_revision.
//
// WHAT IS HASHED ("the db's logical content" = the call/containment graph):
//   - nodes: the SEMANTIC columns — label, name, qualified_name, file_path,
//     start_line, end_line, signature, return_type, is_exported, is_test,
//     language. (parent_id, a surrogate id, is NOT hashed directly; the
//     parent/child CONTAINS edge already carries that linkage into the edge set,
//     so containment is fingerprinted without coupling to AUTOINCREMENT ids.)
//   - edges: type, resolution_method, confidence, candidate_count, evidence_type,
//     verification_status, trust_tier, metadata, source_file, source_line, PLUS
//     both endpoints resolved to their NODE IDENTITY (file_path|qualified_name|
//     name|signature|start_line), not their surrogate source_id/target_id.
//
// WHY id-INDEPENDENT: edge endpoints are hashed by node identity rather than raw
// id, so a from-scratch full index and an incremental `-file` reindex that yield
// the SAME logical graph converge to the SAME revision (AUTOINCREMENT id churn on
// a delete-and-replace never perturbs the fingerprint).
//
// WHAT IS EXCLUDED (volatile / non-logical / would break an invariant):
//   - project_meta — it HOLDS post_revision itself plus build_time_utc / git_commit
//     / go_toolchain; hashing it would be self-referential and non-deterministic.
//   - file_hashes.indexed_at — a wall-clock timestamp.
//   - properties / assertions / closure / cochange / *_fts — derived sidecar
//     caches, not the graph itself. The fingerprint is the CALLS/CONTAINS/IMPORTS
//     graph (nodes + edges); a change to those tables always co-occurs with a
//     node/edge change that the fingerprint already catches.
package store

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"sort"
	"strconv"
)

// revEncodeRecord serialises a row's fields into an UNAMBIGUOUS byte record:
// each field is prefixed by its 8-byte little-endian length, so no field value
// (a signature/path/metadata containing a separator, newline, or NUL) can be
// confused with a field boundary. Deterministic and collision-free at the
// framing level.
func revEncodeRecord(fields ...string) []byte {
	var b bytes.Buffer
	var lb [8]byte
	for _, f := range fields {
		binary.LittleEndian.PutUint64(lb[:], uint64(len(f)))
		b.Write(lb[:])
		b.WriteString(f)
	}
	return b.Bytes()
}

// revBool renders a SQLite boolean (stored 0/1) as a stable token.
func revBool(v bool) string {
	if v {
		return "1"
	}
	return "0"
}

// revConf renders a confidence with FIXED precision so float formatting can
// never drift the hash between two identical runs.
func revConf(c float64) string {
	return strconv.FormatFloat(c, 'f', 6, 64)
}

// nodeIdentity is the id-INDEPENDENT identity of a node — the tuple an edge
// endpoint is hashed by instead of its surrogate id. It is intentionally rich
// (file + qualified_name + name + signature + start_line) so distinct symbols
// that share a simple name remain distinguishable in the fingerprint.
func nodeIdentity(filePath, qname, name, signature string, startLine int) string {
	return revEncodeString(filePath, qname, name, signature, strconv.Itoa(startLine))
}

// revEncodeString is revEncodeRecord as a string key (for map lookups).
func revEncodeString(fields ...string) string {
	return string(revEncodeRecord(fields...))
}

// ComputeRevision returns the hex SHA-256 of graph.db's canonical logical
// dump (see the package doc for exactly what is and is not included).
//
// Determinism: node and edge records are each SORTED (bytes.Compare) before
// they enter the digest, so the result depends only on the SET of hashed
// column values, never on row insertion / scan order.
func (d *DB) ComputeRevision() (string, error) {
	// ── Pass 1: nodes → identity map + canonical node records ──────────────
	nodeRows, err := d.db.Query(
		`SELECT id, label, name, COALESCE(qualified_name,''), file_path,
		        COALESCE(start_line,0), COALESCE(end_line,0),
		        COALESCE(signature,''), COALESCE(return_type,''),
		        is_exported, is_test, language
		   FROM nodes`,
	)
	if err != nil {
		return "", fmt.Errorf("revision: query nodes: %w", err)
	}
	idIdentity := make(map[int64]string)
	var nodeRecs [][]byte
	for nodeRows.Next() {
		var (
			id                          int64
			label, name, qname, file    string
			startLine, endLine          int
			signature, returnType, lang string
			isExported, isTest          bool
		)
		if err := nodeRows.Scan(&id, &label, &name, &qname, &file,
			&startLine, &endLine, &signature, &returnType,
			&isExported, &isTest, &lang); err != nil {
			nodeRows.Close()
			return "", fmt.Errorf("revision: scan node: %w", err)
		}
		idIdentity[id] = nodeIdentity(file, qname, name, signature, startLine)
		nodeRecs = append(nodeRecs, revEncodeRecord(
			"N", label, name, qname, file,
			strconv.Itoa(startLine), strconv.Itoa(endLine),
			signature, returnType, revBool(isExported), revBool(isTest), lang,
		))
	}
	if err := nodeRows.Err(); err != nil {
		nodeRows.Close()
		return "", fmt.Errorf("revision: iterate nodes: %w", err)
	}
	nodeRows.Close()

	// ── Pass 2: edges → canonical edge records (endpoints as node identity) ─
	edgeRows, err := d.db.Query(
		`SELECT source_id, target_id, type, COALESCE(resolution_method,''),
		        COALESCE(confidence,0.0), COALESCE(candidate_count,0),
		        COALESCE(evidence_type,''), COALESCE(verification_status,''),
		        COALESCE(trust_tier,''), COALESCE(metadata,''),
		        COALESCE(source_file,''), COALESCE(source_line,0)
		   FROM edges`,
	)
	if err != nil {
		return "", fmt.Errorf("revision: query edges: %w", err)
	}
	var edgeRecs [][]byte
	for edgeRows.Next() {
		var (
			srcID, tgtID                         int64
			etype, method, evType, vstatus, tier string
			metadata, srcFile                    string
			conf                                 float64
			candCount, srcLine                   int
		)
		if err := edgeRows.Scan(&srcID, &tgtID, &etype, &method,
			&conf, &candCount, &evType, &vstatus, &tier, &metadata,
			&srcFile, &srcLine); err != nil {
			edgeRows.Close()
			return "", fmt.Errorf("revision: scan edge: %w", err)
		}
		// Resolve endpoints to node identity (id-independent). A missing endpoint
		// (orphan — the DeleteFileEdgesAndNodesTx invariant forbids it, but hash
		// defensively) is framed with a stable sentinel so it still contributes.
		srcIdent, ok := idIdentity[srcID]
		if !ok {
			srcIdent = "MISSING:" + strconv.FormatInt(srcID, 10)
		}
		tgtIdent, ok := idIdentity[tgtID]
		if !ok {
			tgtIdent = "MISSING:" + strconv.FormatInt(tgtID, 10)
		}
		edgeRecs = append(edgeRecs, revEncodeRecord(
			"E", etype, method, revConf(conf), strconv.Itoa(candCount),
			evType, vstatus, tier, metadata, srcFile, strconv.Itoa(srcLine),
			srcIdent, tgtIdent,
		))
	}
	if err := edgeRows.Err(); err != nil {
		edgeRows.Close()
		return "", fmt.Errorf("revision: iterate edges: %w", err)
	}
	edgeRows.Close()

	// Sort each record set so SQLite scan order cannot perturb the digest.
	sort.Slice(nodeRecs, func(i, j int) bool { return bytes.Compare(nodeRecs[i], nodeRecs[j]) < 0 })
	sort.Slice(edgeRecs, func(i, j int) bool { return bytes.Compare(edgeRecs[i], edgeRecs[j]) < 0 })

	h := sha256.New()
	// Section tags + counts frame the two record sets so a node record can never
	// be confused with an edge record and an empty graph is still well-defined.
	var lb [8]byte
	binary.LittleEndian.PutUint64(lb[:], uint64(len(nodeRecs)))
	h.Write([]byte("NODES"))
	h.Write(lb[:])
	for _, r := range nodeRecs {
		binary.LittleEndian.PutUint64(lb[:], uint64(len(r)))
		h.Write(lb[:])
		h.Write(r)
	}
	binary.LittleEndian.PutUint64(lb[:], uint64(len(edgeRecs)))
	h.Write([]byte("EDGES"))
	h.Write(lb[:])
	for _, r := range edgeRecs {
		binary.LittleEndian.PutUint64(lb[:], uint64(len(r)))
		h.Write(lb[:])
		h.Write(r)
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// GetMeta reads a project_meta value, returning "" when the key is absent.
func (d *DB) GetMeta(key string) string {
	var v string
	d.db.QueryRow(`SELECT value FROM project_meta WHERE key = ?`, key).Scan(&v)
	return v
}

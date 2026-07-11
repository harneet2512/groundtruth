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
// B-29 (COMPOSITE): post_revision is a COMPOSITE over ALL fact-producing surfaces, not just
// nodes+edges. Each surface has its own sub-revision (see ComputeRevisionParts); the composite
// is the hash over them in a fixed order. This is required because a same-span body / guard /
// docstring / property / assertion / closure / cochange / content edit can change a fact
// while the node+edge columns stay byte-identical — a nodes+edges-only fingerprint would miss
// it and a content/property envelope could never be invalidated. Surfaces hashed:
//
//	nodes · edges · properties · assertions · closure · cochanges · cochange_sets ·
//	content_fts (symbol body content) · receiver (receiver_type provenance) · file_hashes.
//
// Every node reference inside a sidecar is resolved to node IDENTITY (not surrogate id), so
// the composite stays id-independent (a full rebuild and an incremental delete-replace of the
// same logical state converge). Each surface's records are SORTED before hashing, so scan
// order never leaks in.
//
// WHAT IS EXCLUDED (volatile / non-logical / would break an invariant):
//   - project_meta — it HOLDS post_revision itself plus build_time_utc / git_commit
//     / go_toolchain; hashing it would be self-referential and non-deterministic.
//   - file_hashes.indexed_at — a wall-clock timestamp (the content_hash IS hashed).
//   - properties.property_id / properties.source_revision — a derived content id and a
//     back-filled (post-hash) stamp; hashing either would be self-referential.
//   - edge_metadata / content_passages — DERIVED caches of edges.metadata / the symbol
//     bodies (already covered by the edges and content_fts sub-revs); hashing them would
//     double-count without adding sensitivity.
package store

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"sort"
	"strconv"
	"strings"
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

// RevisionParts is the composite revision plus the per-surface sub-revisions it is built
// from (B-29). A consumer keys per-fact-class freshness on the SPECIFIC sub-rev(s) a fact
// depends on (e.g. a localization fact on nodes+edges+content_fts; a property fact on
// nodes+edges+properties), not the whole composite — so a body-only edit invalidates the
// content/property envelopes without churning unrelated fact classes.
type RevisionParts struct {
	Composite string            // hex sha256 over the ordered surface sub-revisions
	Sub       map[string]string // surface name -> hex sha256 sub-revision
}

// revisionSurfaces is the fixed, ordered set of fact-producing surfaces the composite spans.
// Order is load-bearing (the composite frames them in this order). edge_metadata and
// content_passages are DERIVED caches of edges.metadata / the symbol bodies (already covered
// by the edges and content_fts sub-revs) and are intentionally excluded — hashing them would
// double-count without adding sensitivity.
var revisionSurfaces = []string{
	"nodes", "edges", "properties", "assertions", "closure",
	"cochanges", "cochange_sets", "content_fts", "receiver", "file_hashes",
}

// hashRecords frames `tag` + count + the SORTED records into a sha256 and returns it hex.
// Sorting makes the digest depend only on the SET of records, never on SQLite scan order.
func hashRecords(tag string, recs [][]byte) string {
	sort.Slice(recs, func(i, j int) bool { return bytes.Compare(recs[i], recs[j]) < 0 })
	h := sha256.New()
	var lb [8]byte
	h.Write([]byte(tag))
	binary.LittleEndian.PutUint64(lb[:], uint64(len(recs)))
	h.Write(lb[:])
	for _, r := range recs {
		binary.LittleEndian.PutUint64(lb[:], uint64(len(r)))
		h.Write(lb[:])
		h.Write(r)
	}
	return hex.EncodeToString(h.Sum(nil))
}

// identOf resolves a surrogate node id to its id-INDEPENDENT identity, framing a missing
// endpoint (orphan) with a stable sentinel so it still contributes deterministically.
func identOf(idIdentity map[int64]string, id int64) string {
	if s, ok := idIdentity[id]; ok {
		return s
	}
	return "MISSING:" + strconv.FormatInt(id, 10)
}

// ComputeRevision returns the composite content revision (B-29): the hex SHA-256 over ALL
// fact-producing surfaces, so a same-span property / assertion / closure / cochange / body
// (content_fts) edit that leaves the node+edge columns byte-identical STILL moves the
// revision. Determinism + id-independence are preserved: every surface's records are sorted
// before hashing and all node references are resolved to node identity, not surrogate id.
func (d *DB) ComputeRevision() (string, error) {
	parts, err := d.ComputeRevisionParts()
	if err != nil {
		return "", err
	}
	return parts.Composite, nil
}

// ComputeRevisionParts computes every surface sub-revision and the composite over them.
func (d *DB) ComputeRevisionParts() (RevisionParts, error) {
	idIdentity, nodeRecs, err := d.revNodeRecords()
	if err != nil {
		return RevisionParts{}, err
	}
	sub := make(map[string]string, len(revisionSurfaces))
	sub["nodes"] = hashRecords("NODES", nodeRecs)

	edgeRecs, recvRecs, err := d.revEdgeRecords(idIdentity)
	if err != nil {
		return RevisionParts{}, err
	}
	sub["edges"] = hashRecords("EDGES", edgeRecs)
	sub["receiver"] = hashRecords("RECV", recvRecs)

	for surface, fn := range map[string]func(map[int64]string) ([][]byte, error){
		"properties":    d.revPropertyRecords,
		"assertions":    d.revAssertionRecords,
		"closure":       d.revClosureRecords,
		"cochanges":     func(_ map[int64]string) ([][]byte, error) { return d.revCochangeRecords() },
		"cochange_sets": func(_ map[int64]string) ([][]byte, error) { return d.revCochangeSetRecords() },
		"content_fts":   d.revContentFTSRecords,
		"file_hashes":   func(_ map[int64]string) ([][]byte, error) { return d.revFileHashRecords() },
	} {
		recs, ferr := fn(idIdentity)
		if ferr != nil {
			return RevisionParts{}, fmt.Errorf("revision: %s: %w", surface, ferr)
		}
		sub[surface] = hashRecords(strings.ToUpper(surface), recs)
	}

	// Composite = sha256 over the surface sub-revisions in the fixed order.
	h := sha256.New()
	for _, s := range revisionSurfaces {
		rec := revEncodeRecord(s, sub[s])
		var lb [8]byte
		binary.LittleEndian.PutUint64(lb[:], uint64(len(rec)))
		h.Write(lb[:])
		h.Write(rec)
	}
	return RevisionParts{Composite: hex.EncodeToString(h.Sum(nil)), Sub: sub}, nil
}

// revNodeRecords returns the id→identity map plus the canonical node records (the SEMANTIC
// columns; parent_id is excluded — the parent/child CONTAINS edge already carries it).
func (d *DB) revNodeRecords() (map[int64]string, [][]byte, error) {
	rows, err := d.db.Query(
		`SELECT id, label, name, COALESCE(qualified_name,''), file_path,
		        COALESCE(start_line,0), COALESCE(end_line,0),
		        COALESCE(signature,''), COALESCE(return_type,''),
		        is_exported, is_test, language
		   FROM nodes`,
	)
	if err != nil {
		return nil, nil, fmt.Errorf("query nodes: %w", err)
	}
	defer rows.Close()
	idIdentity := make(map[int64]string)
	var recs [][]byte
	for rows.Next() {
		var (
			id                          int64
			label, name, qname, file    string
			startLine, endLine          int
			signature, returnType, lang string
			isExported, isTest          bool
		)
		if err := rows.Scan(&id, &label, &name, &qname, &file,
			&startLine, &endLine, &signature, &returnType,
			&isExported, &isTest, &lang); err != nil {
			return nil, nil, fmt.Errorf("scan node: %w", err)
		}
		idIdentity[id] = nodeIdentity(file, qname, name, signature, startLine)
		recs = append(recs, revEncodeRecord(
			"N", label, name, qname, file,
			strconv.Itoa(startLine), strconv.Itoa(endLine),
			signature, returnType, revBool(isExported), revBool(isTest), lang,
		))
	}
	return idIdentity, recs, rows.Err()
}

// revEdgeRecords returns the canonical edge records (endpoints as identity) AND the receiver
// provenance records (the receiver_type tag on edges.metadata, exposed as its own surface).
func (d *DB) revEdgeRecords(idIdentity map[int64]string) (edgeRecs, recvRecs [][]byte, err error) {
	rows, err := d.db.Query(
		`SELECT source_id, target_id, type, COALESCE(resolution_method,''),
		        COALESCE(confidence,0.0), COALESCE(candidate_count,0),
		        COALESCE(evidence_type,''), COALESCE(verification_status,''),
		        COALESCE(trust_tier,''), COALESCE(metadata,''),
		        COALESCE(source_file,''), COALESCE(source_line,0)
		   FROM edges`,
	)
	if err != nil {
		return nil, nil, fmt.Errorf("query edges: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var (
			srcID, tgtID                         int64
			etype, method, evType, vstatus, tier string
			metadata, srcFile                    string
			conf                                 float64
			candCount, srcLine                   int
		)
		if err := rows.Scan(&srcID, &tgtID, &etype, &method,
			&conf, &candCount, &evType, &vstatus, &tier, &metadata,
			&srcFile, &srcLine); err != nil {
			return nil, nil, fmt.Errorf("scan edge: %w", err)
		}
		srcIdent := identOf(idIdentity, srcID)
		tgtIdent := identOf(idIdentity, tgtID)
		edgeRecs = append(edgeRecs, revEncodeRecord(
			"E", etype, method, revConf(conf), strconv.Itoa(candCount),
			evType, vstatus, tier, metadata, srcFile, strconv.Itoa(srcLine),
			srcIdent, tgtIdent,
		))
		if rt := ParseEdgeMetadata(metadata)["receiver_type"]; rt != "" {
			recvRecs = append(recvRecs, revEncodeRecord("R", srcIdent, tgtIdent, rt))
		}
	}
	return edgeRecs, recvRecs, rows.Err()
}

// revPropertyRecords hashes the property facts by node IDENTITY (id-independent). property_id
// (a derived content hash) and source_revision (back-filled AFTER this hash, so self-
// referential) are EXCLUDED; every other semantic/provenance column is hashed, so a change
// to a value, span, tier, or extractor moves the properties sub-rev.
func (d *DB) revPropertyRecords(idIdentity map[int64]string) ([][]byte, error) {
	rows, err := d.db.Query(
		`SELECT node_id, kind, value, COALESCE(line,0),
		        COALESCE(start_line, line, 0), COALESCE(end_line, line, 0),
		        COALESCE(extractor,''), COALESCE(evidence_method,''),
		        COALESCE(confidence,0.0), COALESCE(trust_tier,''),
		        COALESCE(verification_status,'')
		   FROM properties`,
	)
	if err != nil {
		return nil, fmt.Errorf("query properties: %w", err)
	}
	defer rows.Close()
	var recs [][]byte
	for rows.Next() {
		var (
			nodeID                          int64
			kind, value                     string
			line, startLine, endLine        int
			extractor, evMethod, tier, vsts string
			conf                            float64
		)
		if err := rows.Scan(&nodeID, &kind, &value, &line, &startLine, &endLine,
			&extractor, &evMethod, &conf, &tier, &vsts); err != nil {
			return nil, fmt.Errorf("scan property: %w", err)
		}
		recs = append(recs, revEncodeRecord(
			"P", identOf(idIdentity, nodeID), kind, value,
			strconv.Itoa(startLine), strconv.Itoa(endLine),
			extractor, evMethod, revConf(conf), tier, vsts,
		))
	}
	return recs, rows.Err()
}

// revAssertionRecords hashes assertion facts by test/target node identity.
func (d *DB) revAssertionRecords(idIdentity map[int64]string) ([][]byte, error) {
	rows, err := d.db.Query(
		`SELECT test_node_id, COALESCE(target_node_id,0), COALESCE(resolution_score,0.0),
		        kind, expression, COALESCE(expected,''), COALESCE(line,0)
		   FROM assertions`,
	)
	if err != nil {
		return nil, fmt.Errorf("query assertions: %w", err)
	}
	defer rows.Close()
	var recs [][]byte
	for rows.Next() {
		var (
			testID, targetID     int64
			score                float64
			kind, expr, expected string
			line                 int
		)
		if err := rows.Scan(&testID, &targetID, &score, &kind, &expr, &expected, &line); err != nil {
			return nil, fmt.Errorf("scan assertion: %w", err)
		}
		recs = append(recs, revEncodeRecord(
			"A", identOf(idIdentity, testID), identOf(idIdentity, targetID),
			revConf(score), kind, expr, expected, strconv.Itoa(line),
		))
	}
	return recs, rows.Err()
}

// revClosureRecords hashes closure (transitive reachability) facts by endpoint identity.
func (d *DB) revClosureRecords(idIdentity map[int64]string) ([][]byte, error) {
	rows, err := d.db.Query(
		`SELECT source_id, target_id, COALESCE(depth,0), COALESCE(min_confidence,0.0) FROM closure`,
	)
	if err != nil {
		return nil, fmt.Errorf("query closure: %w", err)
	}
	defer rows.Close()
	var recs [][]byte
	for rows.Next() {
		var (
			srcID, tgtID int64
			depth        int
			minConf      float64
		)
		if err := rows.Scan(&srcID, &tgtID, &depth, &minConf); err != nil {
			return nil, fmt.Errorf("scan closure: %w", err)
		}
		recs = append(recs, revEncodeRecord(
			"C", identOf(idIdentity, srcID), identOf(idIdentity, tgtID),
			strconv.Itoa(depth), revConf(minConf),
		))
	}
	return recs, rows.Err()
}

// revCochangeRecords hashes the legacy pair-form co-change table (keyed by file path — id-
// independent by construction).
func (d *DB) revCochangeRecords() ([][]byte, error) {
	rows, err := d.db.Query(`SELECT file_a, file_b, COALESCE(count,0) FROM cochanges`)
	if err != nil {
		return nil, fmt.Errorf("query cochanges: %w", err)
	}
	defer rows.Close()
	var recs [][]byte
	for rows.Next() {
		var fileA, fileB string
		var count int
		if err := rows.Scan(&fileA, &fileB, &count); err != nil {
			return nil, fmt.Errorf("scan cochange: %w", err)
		}
		recs = append(recs, revEncodeRecord("CC", fileA, fileB, strconv.Itoa(count)))
	}
	return recs, rows.Err()
}

// revCochangeSetRecords hashes the DCC set-form co-change membership (keyed by commit+path).
func (d *DB) revCochangeSetRecords() ([][]byte, error) {
	rows, err := d.db.Query(`SELECT commit_hash, file_path FROM cochange_sets`)
	if err != nil {
		return nil, fmt.Errorf("query cochange_sets: %w", err)
	}
	defer rows.Close()
	var recs [][]byte
	for rows.Next() {
		var commit, file string
		if err := rows.Scan(&commit, &file); err != nil {
			return nil, fmt.Errorf("scan cochange_set: %w", err)
		}
		recs = append(recs, revEncodeRecord("CS", commit, file))
	}
	return recs, rows.Err()
}

// revContentFTSRecords hashes the symbol body-content surface (symbol_content_fts) by node
// identity — THE surface that catches a same-span body edit (the node columns are
// unchanged). FTS5-gated: absent table -> empty record set (correct-or-quiet).
func (d *DB) revContentFTSRecords(idIdentity map[int64]string) ([][]byte, error) {
	var n int
	if err := d.db.QueryRow(
		"SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='symbol_content_fts'",
	).Scan(&n); err != nil || n == 0 {
		return nil, nil
	}
	rows, err := d.db.Query(`SELECT rowid, content FROM symbol_content_fts`)
	if err != nil {
		return nil, fmt.Errorf("query symbol_content_fts: %w", err)
	}
	defer rows.Close()
	var recs [][]byte
	for rows.Next() {
		var rowid int64
		var content string
		if err := rows.Scan(&rowid, &content); err != nil {
			return nil, fmt.Errorf("scan content_fts: %w", err)
		}
		recs = append(recs, revEncodeRecord("F", identOf(idIdentity, rowid), content))
	}
	return recs, rows.Err()
}

// revFileHashRecords hashes the per-file content hashes (file_path, content_hash, language).
// indexed_at is EXCLUDED — it is wall-clock and would break byte-determinism.
func (d *DB) revFileHashRecords() ([][]byte, error) {
	rows, err := d.db.Query(`SELECT file_path, content_hash, COALESCE(language,'') FROM file_hashes`)
	if err != nil {
		return nil, fmt.Errorf("query file_hashes: %w", err)
	}
	defer rows.Close()
	var recs [][]byte
	for rows.Next() {
		var path, hash, lang string
		if err := rows.Scan(&path, &hash, &lang); err != nil {
			return nil, fmt.Errorf("scan file_hash: %w", err)
		}
		recs = append(recs, revEncodeRecord("H", path, hash, lang))
	}
	return recs, rows.Err()
}

// StampCompositeRevision computes the composite revision + per-surface sub-revisions, stamps
// `post_revision` and each `subrev_<surface>` into project_meta, and back-fills every live
// property's `source_revision` with the composite. source_revision is EXCLUDED from the props
// sub-rev hash, so the back-fill is revision-neutral (recomputing the revision afterwards
// yields the same value). Returns the composite. Consumers key per-fact-class freshness on
// the specific subrev_<surface> keys.
func (d *DB) StampCompositeRevision() (string, error) {
	parts, err := d.ComputeRevisionParts()
	if err != nil {
		return "", fmt.Errorf("compute composite revision: %w", err)
	}
	if err := d.SetMeta("post_revision", parts.Composite); err != nil {
		return "", fmt.Errorf("stamp post_revision: %w", err)
	}
	for _, s := range revisionSurfaces {
		if err := d.SetMeta("subrev_"+s, parts.Sub[s]); err != nil {
			return "", fmt.Errorf("stamp subrev_%s: %w", s, err)
		}
	}
	if _, err := d.db.Exec(`UPDATE properties SET source_revision = ?`, parts.Composite); err != nil {
		return "", fmt.Errorf("backfill property source_revision: %w", err)
	}
	return parts.Composite, nil
}

// GetMeta reads a project_meta value, returning "" when the key is absent.
func (d *DB) GetMeta(key string) string {
	var v string
	d.db.QueryRow(`SELECT value FROM project_meta WHERE key = ?`, key).Scan(&v)
	return v
}

package community

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"math"
)

// This file owns the community tables outright. They are NEW tables, created
// here with CREATE TABLE IF NOT EXISTS, because internal/store/sqlite.go is a
// protected producer path this item does not touch. The cost is stated: the
// schema lives in two places if it is ever migrated into sqlite.go, and
// TestSchemaRoundTrip is what would catch the drift.
//
// The only write statements in this package are the two CREATE TABLE
// statements below and the two INSERTs. There is no UPDATE anywhere, and
// nothing here names `edges`, `nodes` or `trust_tier` in a writing position.
// That is the enforcement behind the invariant in the package doc: community
// membership cannot promote a candidate edge because there is no code path
// that can write to an edge at all.

// MemberKindFile is the member_kind stored for every row this package writes.
// The column exists because a later item may cluster symbol stable_ids; a
// reader must never have to guess which granularity a row is at.
const MemberKindFile = "file"

// Schema is the DDL for the two tables, additive and idempotent. It is
// exported so a caller can create the tables in its own migration step and so
// the exact text is reviewable in one place.
//
// Cohesion columns are nullable ON PURPOSE. An unmeasurable held-out cohesion
// is stored as NULL with cohesion_reason explaining it -- never as 0.0, which
// would read as a measured total failure, and never as an omitted row, which
// would read as a community that was never found.
const Schema = `
	CREATE TABLE IF NOT EXISTS communities (
		id TEXT PRIMARY KEY,
		label TEXT NOT NULL,
		heuristic_label TEXT NOT NULL,
		keywords TEXT NOT NULL,
		description TEXT NOT NULL,
		enriched_by TEXT NOT NULL,
		cohesion REAL,
		cohesion_lo REAL,
		cohesion_hi REAL,
		cohesion_n INTEGER NOT NULL DEFAULT 0,
		cohesion_reason TEXT NOT NULL,
		structural_cohesion REAL NOT NULL,
		member_count INTEGER NOT NULL,
		internal_weight REAL NOT NULL,
		external_weight REAL NOT NULL,
		evidence_edge_ids TEXT NOT NULL,
		evidence_truncated INTEGER NOT NULL DEFAULT 0,
		algorithm TEXT NOT NULL,
		resolution REAL NOT NULL,
		w_call REAL NOT NULL,
		w_cochange REAL NOT NULL,
		holdout_commits INTEGER NOT NULL DEFAULT 0
	);

	CREATE TABLE IF NOT EXISTS community_members (
		community_id TEXT NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
		member TEXT NOT NULL,
		member_kind TEXT NOT NULL DEFAULT 'file',
		PRIMARY KEY(community_id, member)
	);

	CREATE INDEX IF NOT EXISTS idx_community_members_member ON community_members(member);
	CREATE INDEX IF NOT EXISTS idx_communities_label ON communities(heuristic_label);
`

// CommunityColumns names the columns Persist writes, in schema order. Naming
// them once means a schema drift shows up as a review diff rather than as a
// runtime "no such column" during publication.
const CommunityColumns = "id, label, heuristic_label, keywords, description, enriched_by, " +
	"cohesion, cohesion_lo, cohesion_hi, cohesion_n, cohesion_reason, structural_cohesion, " +
	"member_count, internal_weight, external_weight, evidence_edge_ids, evidence_truncated, " +
	"algorithm, resolution, w_call, w_cochange, holdout_commits"

// MemberColumns names the community_members columns Persist writes.
const MemberColumns = "community_id, member, member_kind"

const insertCommunitySQL = "INSERT OR REPLACE INTO communities (" + CommunityColumns +
	") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"

const insertMemberSQL = "INSERT OR REPLACE INTO community_members (" + MemberColumns + ") VALUES (?, ?, ?)"

// EnsureSchema creates the community tables if they do not exist. Persist
// calls it, so a caller only needs it directly when it wants the tables to
// exist before any community does -- which is the difference between "analysis
// found nothing" and "analysis never ran".
func EnsureSchema(tx *sql.Tx) error {
	if tx == nil {
		return fmt.Errorf("community: ensure schema: nil transaction")
	}
	if _, err := tx.Exec(Schema); err != nil {
		return fmt.Errorf("community: create community tables: %w", err)
	}
	return nil
}

// Persist writes result.Communities and their members inside the caller's
// transaction and returns the number of community rows written.
//
// It takes the transaction rather than opening one so the partition lands in
// the same atomic publication as whatever else the caller is committing: a
// graph that commits must not be able to commit half a partition.
//
// A Result with no communities still creates the tables and writes nothing,
// returning 0 without error. Empty-but-present is the shape that lets a reader
// distinguish "clustered, found nothing" from "never clustered"; the Reason
// belongs in the receipt, not in a data row.
func Persist(tx *sql.Tx, result Result) (int, error) {
	if tx == nil {
		return 0, fmt.Errorf("community: persist: nil transaction")
	}
	if err := EnsureSchema(tx); err != nil {
		return 0, err
	}
	if len(result.Communities) == 0 {
		return 0, nil
	}

	commStmt, err := tx.Prepare(insertCommunitySQL)
	if err != nil {
		return 0, fmt.Errorf("community: prepare communities insert: %w", err)
	}
	defer commStmt.Close()
	memberStmt, err := tx.Prepare(insertMemberSQL)
	if err != nil {
		return 0, fmt.Errorf("community: prepare community_members insert: %w", err)
	}
	defer memberStmt.Close()

	opts := result.Options.Normalize()
	written := 0
	// result.Communities is already in its deterministic published order, so
	// two publications of the same graph produce the same page layout.
	for _, c := range result.Communities {
		keywordsJSON, err := encodeStrings(c.Keywords)
		if err != nil {
			return written, fmt.Errorf("community: encode keywords for %s: %w", c.ID, err)
		}
		evidenceJSON, err := encodeStrings(c.EvidenceEdgeIDs)
		if err != nil {
			return written, fmt.Errorf("community: encode evidence for %s: %w", c.ID, err)
		}
		if _, err := commStmt.Exec(
			c.ID, c.Label, c.HeuristicLabel, keywordsJSON, c.Description, c.EnrichedBy,
			nullable(c.Cohesion), nullable(c.CohesionInterval.Lo), nullable(c.CohesionInterval.Hi),
			c.CohesionInterval.N, c.CohesionReason, c.StructuralCohesion,
			len(c.Members), c.InternalWeight, c.ExternalWeight, evidenceJSON, boolToInt(c.EvidenceTruncated),
			result.Algorithm, opts.Resolution, opts.WCall, opts.WCochange, opts.HoldoutCommits,
		); err != nil {
			return written, fmt.Errorf("community: insert community %s: %w", c.ID, err)
		}
		for _, m := range c.Members {
			if _, err := memberStmt.Exec(c.ID, m, MemberKindFile); err != nil {
				return written, fmt.Errorf("community: insert member %s of %s: %w", m, c.ID, err)
			}
		}
		written++
	}
	return written, nil
}

// Load reads back what Persist wrote, in the published order. It exists so the
// round trip is testable and so a consumer does not have to re-implement the
// NULL-versus-NaN convention and get it subtly wrong.
func Load(q RowQueryer) ([]Community, error) {
	if q == nil {
		return nil, fmt.Errorf("community: load: nil queryer")
	}
	rows, err := q.Query("SELECT " + CommunityColumns + " FROM communities ORDER BY member_count DESC, id ASC")
	if err != nil {
		return nil, fmt.Errorf("community: read communities: %w", err)
	}
	defer rows.Close()

	var out []Community
	for rows.Next() {
		var (
			c                                Community
			keywordsJSON, evidenceJSON       string
			cohesion, cohesionLo, cohesionHi sql.NullFloat64
			memberCount, truncated, holdout  int
			algorithm                        string
			resolution, wCall, wCochange     float64
		)
		if err := rows.Scan(
			&c.ID, &c.Label, &c.HeuristicLabel, &keywordsJSON, &c.Description, &c.EnrichedBy,
			&cohesion, &cohesionLo, &cohesionHi, &c.CohesionInterval.N, &c.CohesionReason,
			&c.StructuralCohesion, &memberCount, &c.InternalWeight, &c.ExternalWeight,
			&evidenceJSON, &truncated, &algorithm, &resolution, &wCall, &wCochange, &holdout,
		); err != nil {
			return nil, fmt.Errorf("community: scan community row: %w", err)
		}
		c.Cohesion = fromNullable(cohesion)
		c.CohesionInterval.Lo = fromNullable(cohesionLo)
		c.CohesionInterval.Hi = fromNullable(cohesionHi)
		c.EvidenceTruncated = truncated != 0
		if err := json.Unmarshal([]byte(keywordsJSON), &c.Keywords); err != nil {
			return nil, fmt.Errorf("community: decode keywords for %s: %w", c.ID, err)
		}
		if err := json.Unmarshal([]byte(evidenceJSON), &c.EvidenceEdgeIDs); err != nil {
			return nil, fmt.Errorf("community: decode evidence for %s: %w", c.ID, err)
		}
		out = append(out, c)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("community: iterate community rows: %w", err)
	}

	for i := range out {
		members, err := loadMembers(q, out[i].ID)
		if err != nil {
			return nil, err
		}
		out[i].Members = members
	}
	return out, nil
}

// RowQueryer is the read surface Load needs. Both *sql.DB and *sql.Tx satisfy
// it.
type RowQueryer interface {
	Query(query string, args ...interface{}) (*sql.Rows, error)
}

func loadMembers(q RowQueryer, id string) ([]string, error) {
	rows, err := q.Query("SELECT member FROM community_members WHERE community_id = ? ORDER BY member ASC", id)
	if err != nil {
		return nil, fmt.Errorf("community: read members of %s: %w", id, err)
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var m string
		if err := rows.Scan(&m); err != nil {
			return nil, fmt.Errorf("community: scan member of %s: %w", id, err)
		}
		out = append(out, m)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("community: iterate members of %s: %w", id, err)
	}
	return out, nil
}

// encodeStrings renders a list as a JSON array, and an empty or nil list as
// "[]" rather than "null", so a consumer never has to handle two spellings of
// nothing.
func encodeStrings(v []string) (string, error) {
	if len(v) == 0 {
		return "[]", nil
	}
	b, err := json.Marshal(v)
	if err != nil {
		return "", err
	}
	return string(b), nil
}

// nullable maps NaN to SQL NULL. This is the one conversion that carries the
// whole "never fabricated" promise into the database: an unmeasurable cohesion
// must not arrive as a number.
func nullable(v float64) interface{} {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return nil
	}
	return v
}

// fromNullable is nullable's inverse: SQL NULL reads back as NaN, not as 0.
func fromNullable(v sql.NullFloat64) float64 {
	if !v.Valid {
		return math.NaN()
	}
	return v.Float64
}

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

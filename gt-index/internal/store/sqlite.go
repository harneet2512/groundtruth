// Package store handles SQLite graph database operations.
package store

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	_ "github.com/mattn/go-sqlite3"
)

// DB wraps an SQLite database for the code graph.
type DB struct {
	db *sql.DB
}

// Node represents a code entity (function, class, method, etc.)
type Node struct {
	ID            int64
	Label         string // Function, Class, Method, File, Interface, Struct, Enum, Type
	Name          string
	QualifiedName string
	FilePath      string
	StartLine     int
	EndLine       int
	Signature     string
	ReturnType    string
	IsExported    bool
	IsTest        bool
	Language      string
	ParentID      int64
}

func nullableParentID(parentID int64) any {
	if parentID <= 0 {
		return nil
	}
	return parentID
}

// Edge represents a relationship between nodes.
type Edge struct {
	ID                 int64
	SourceID           int64
	TargetID           int64
	Type               string // CALLS, IMPORTS, DEFINES, INHERITS, IMPLEMENTS
	SourceLine         int
	SourceFile         string
	ResolutionMethod   string // same_file, import, name_match
	Confidence         float64
	Metadata           string
	TrustTier          string // CERTIFIED, CANDIDATE, SPECULATIVE, SUPPRESSED
	CandidateCount     int
	EvidenceType       string // ast_call, ast_import, name_match
	VerificationStatus string // unverified, verified, rejected
}

// ResolutionCandidate preserves one resolver-produced viable target. The
// legacy edges table remains backward-readable, while this additive surface
// prevents ambiguity from collapsing into a scalar count.
type ResolutionCandidate struct {
	CallsiteID          string
	TargetID            int64
	TargetStableID      string
	TargetNativeID      string
	Ordinal             int
	Mechanism           string
	DeclaredScope       string
	ReceiverType        string
	ReceiverOrigin      string
	ReceiverShape       string
	ReceiverChain       string
	ImportChain         string
	FlowSourceStableIDs []string
	FlowEdgeStableIDs   []string
	FlowSourceFacts     []ResolutionFlowFact
	FlowEdgeFacts       []ResolutionFlowFact
	DynamicDispatch     bool
	ExportStatus        string
	ParserComplete      *bool
	VerificationStatus  string
	Selected            bool
}

// ResolutionFlowFact is a typed, candidate-bound VTA source or propagation
// fact. StableID is the immutable identity referenced by a derivation fact;
// the remaining fields make the reference auditable at the graph boundary.
type ResolutionFlowFact struct {
	StableID       string
	Kind           string
	CallsiteID     string
	TargetStableID string
	Payload        string
}

type ResolutionCallsite struct {
	CallsiteID             string
	CallsiteOrdinal        int
	RepositoryRevision     string
	SourceStableID         string
	SourceNativeID         string
	SourceID               int64
	SourceLine             int
	SourceFile             string
	Callee                 string
	Language               string
	DispatchState          string
	CandidateCount         int
	SelectedTargetStableID *string
	SelectedTargetNativeID *string
	Mechanism              string
	VerificationStatus     string
	RepoID                 string
	FileNodeID             int64
	FileIdentity           string
	CallerSymbolID         string
	ASTPath                string
	ByteStart              uint64
	ByteEnd                uint64
	ColumnStart            uint32
	DispatchForm           string
	ArgumentArity          *uint16
	ParseState             string
	PassCoverage           []ResolutionPassCoverage
}

// AttachedResolution is the graph-native representation of a resolver callsite.
// The Callsite node and its HAS_CALLSITE/CANDIDATE edges live in the primary
// nodes/edges graph; the resolution_* tables are only a compatibility projection
// for older consumers and are never the authority for graph queries.
type AttachedResolution struct {
	Callsite   *ResolutionCallsite
	Source     *Node
	Candidates []*ResolutionCandidate
}

// AttachedCandidate is returned by the normal graph query path. It intentionally
// reads nodes/edges rather than the compatibility sidecar so callers cannot forget
// to consume the attached evidence.
type AttachedCandidate struct {
	CallsiteID          string
	SourceID            int64
	CallsiteNodeID      int64
	TargetID            int64
	TargetStableID      string
	HasCandidate        bool
	CandidateCount      int
	SourceFile          string
	SourceLine          int
	Callee              string
	DispatchState       string
	CandidateOrdinal    int
	Mechanism           string
	Revision            string
	StructuralAuthority string
	TargetAuthority     string
	DerivationKind      string
	EvidenceSet         string
	SiblingCount        int
	AbstentionReason    string
	DeclaredScope       string
	ReceiverType        string
	ReceiverOrigin      string
	ReceiverShape       string
	ReceiverChain       string
	ImportChain         string
	FlowSourceStableIDs []string
	FlowEdgeStableIDs   []string
	FlowSourceFacts     []ResolutionFlowFact
	FlowEdgeFacts       []ResolutionFlowFact
	ExportStatus        string
	ParserComplete      *bool
	PolicyID            string
	PolicyVersion       string
	PolicyVersionID     string
	PolicyRank          int
	PolicySelected      bool
	PolicyReason        string
	MatchedRuleIDs      []string
	FactIDsRead         []string
	CompletenessStates  map[string]string
	DerivedScore        *string
	PolicyHash          string
	PassCoverage        []ResolutionPassCoverage
	ProvenanceSteps     []ResolutionDerivationStep
}

const GraphCompletionSchema = "gt-index.graph-completion.v1"
const ResolutionDerivationContract = "gt-index.call-resolution.derivation.v2"
const CallResolutionSchemaVersion = 2

var dispatchFormsV2 = map[string]struct{}{
	"static": {}, "special": {}, "virtual": {}, "interface": {}, "function_value": {},
	"dynamic_name": {}, "reflection": {}, "di_lookup": {}, "ffi": {}, "unknown": {},
}

var derivationPassKindsV2 = map[string]struct{}{
	"direct_binding": {}, "cha": {}, "rta": {}, "vta": {},
	"points_to_field_insensitive": {}, "points_to_field_sensitive": {}, "on_the_fly_refinement": {},
	"import_binding": {}, "scope_binding": {}, "return_shape": {}, "unique_name_fallback": {},
	"framework_route": {}, "di_binding": {}, "reflection_model": {}, "higher_order_flow": {},
	"ffi_contract": {}, "generated_mapping": {}, "legacy_unknown": {},
}

func canonicalResolutionID(fields ...string) string {
	sum := sha256.Sum256([]byte(strings.Join(fields, "\x00")))
	return hex.EncodeToString(sum[:])
}

func StableCallsiteV2ID(repoID, revision, fileIdentity, astPath string, byteStart, byteEnd uint64, dispatchForm, calleeLexeme string) (string, error) {
	if repoID == "" || revision == "" || fileIdentity == "" || astPath == "" || byteStart >= byteEnd {
		return "", fmt.Errorf("incomplete v2 callsite identity")
	}
	if _, ok := dispatchFormsV2[dispatchForm]; !ok {
		return "", fmt.Errorf("unknown dispatch form %q", dispatchForm)
	}
	return canonicalResolutionID(repoID, revision, fileIdentity, astPath, strconv.FormatUint(byteStart, 10), strconv.FormatUint(byteEnd, 10), dispatchForm, calleeLexeme), nil
}

func stableCandidateV2ID(callsiteID, targetStableID string) string {
	return canonicalResolutionID(callsiteID, targetStableID)
}

type CandidateQueryPolicy struct {
	ID      string
	Version string
}

func (p CandidateQueryPolicy) VersionID() string {
	return canonicalResolutionID(p.CanonicalJSON())
}

func (p CandidateQueryPolicy) CanonicalJSON() string {
	rules := `[{"action":"include","predicate":{"candidate_count":1,"completeness":"closed","target_authority":"source_supported"},"priority":10,"rule_id":"select_closed_source_supported"}]`
	if p == InspectAllCandidatePolicy {
		rules = `[{"action":"abstain","predicate":{"always":true},"priority":10,"rule_id":"inspect_without_selection"}]`
	}
	return fmt.Sprintf(`{"explanation_template_version":"1","fact_schema_max":2,"fact_schema_min":2,"policy_name":%q,"rules":%s,"semantic_version":%q,"tie_breakers":["target_symbol_id"]}`, p.ID, rules, p.Version)
}

func (p CandidateQueryPolicy) SelectionRuleID() string {
	if p == ConservativeCandidatePolicy {
		return "select_closed_source_supported"
	}
	return "inspect_without_selection"
}

type ResolutionPassCoverage struct {
	FactID                  string   `json:"fact_id,omitempty"`
	PassKind                string   `json:"pass_kind"`
	Version                 string   `json:"version"`
	Status                  string   `json:"status"`
	Reason                  string   `json:"reason,omitempty"`
	CandidateStableIDs      []string `json:"candidate_stable_ids,omitempty"`
	FlowTypeStableIDs       []string `json:"flow_type_stable_ids,omitempty"`
	AllocationTypeStableIDs []string `json:"allocation_type_stable_ids,omitempty"`
	ReachableStableIDs      []string `json:"reachable_stable_ids,omitempty"`
	RootStableIDs           []string `json:"root_stable_ids,omitempty"`
	RootPolicy              string   `json:"root_policy,omitempty"`
	RootPolicyVersion       string   `json:"root_policy_version,omitempty"`
	RootCompleteness        string   `json:"root_completeness,omitempty"`
	RootBoundary            string   `json:"root_boundary,omitempty"`
}

type ResolutionDerivationStep struct {
	StepID string `json:"step_id"`
	Kind   string `json:"kind"`
	Value  string `json:"value"`
}

var ConservativeCandidatePolicy = CandidateQueryPolicy{ID: "gt-index.candidate.conservative", Version: "1"}
var InspectAllCandidatePolicy = CandidateQueryPolicy{ID: "gt-index.candidate.inspect-all", Version: "1"}

func (p CandidateQueryPolicy) validate() error {
	if p != ConservativeCandidatePolicy && p != InspectAllCandidatePolicy {
		return fmt.Errorf("unknown candidate query policy %q/%q", p.ID, p.Version)
	}
	return nil
}

func resolutionDerivation(mechanism, dispatchState string, candidateCount int) (kind, evidenceSet, abstentionReason string, err error) {
	if dispatchState == "dynamic" {
		return "dynamic_dispatch", "partial", "dynamic_target_not_statically_proven", nil
	}
	if dispatchState == "parser_incomplete" {
		return "syntax_incomplete", "partial", "parser_incomplete", nil
	}
	if dispatchState == "zero" || dispatchState == "external_unresolved" {
		return "unresolved", "partial", dispatchState, nil
	}
	switch mechanism {
	case "same_file":
		return "lexical_binding", "closed", "", nil
	case "inherited":
		return "declared_type", "closed", "", nil
	case "import":
		return "import_binding", "closed", "", nil
	case "import_type", "type_flow", "return_type":
		return "declared_type", "closed", "", nil
	case "vta":
		return "variable_type_flow", "open", "candidate_only_flow_evidence", nil
	case "impl_method":
		if candidateCount == 1 {
			return "single_implementation", "closed", "", nil
		}
		return "interface_implementors", "closed", "", nil
	case "verified_unique", "name_match", "unknown_legacy", "external", "":
		return "global_name", "partial", "", nil
	default:
		return "", "", "", fmt.Errorf("unknown resolution derivation mechanism %q", mechanism)
	}
}

func resolutionCoverage(c *ResolutionCallsite) ([]ResolutionPassCoverage, []ResolutionDerivationStep) {
	coverage := append([]ResolutionPassCoverage(nil), c.PassCoverage...)
	if len(coverage) == 0 {
		// Compatibility callers construct rows without invoking the resolver.
		// Absence of a trace is not evidence that any static pass ran.
		for _, pass := range []string{"lexical_binding", "import_binding", "declared_type", "implementation_set", "return_type", "global_name", "dynamic_framework"} {
			entry := ResolutionPassCoverage{PassKind: pass, Version: "1", Status: "not_run", Reason: "execution_trace_unavailable"}
			if pass == "dynamic_framework" && c.DispatchState == "dynamic" {
				entry.Status, entry.Reason = "unavailable", "static_target_not_proven"
			}
			coverage = append(coverage, entry)
		}
	}
	winner := ""
	for i := range coverage {
		if coverage[i].Version == "" {
			coverage[i].Version = "1"
		}
		if coverage[i].Status == "completed_match" {
			winner = coverage[i].PassKind
		}
	}
	steps := []ResolutionDerivationStep{
		{StepID: "01:callsite", Kind: "callsite_fact", Value: c.DispatchState},
		{StepID: "02:pass", Kind: "resolution_pass", Value: winner},
		{StepID: "03:outcome", Kind: "candidate_set", Value: c.Mechanism},
	}
	return coverage, steps
}

func candidateProvenance(base []ResolutionDerivationStep, candidate *ResolutionCandidate) []ResolutionDerivationStep {
	steps := append([]ResolutionDerivationStep(nil), base...)
	facts := []struct{ kind, value string }{
		{"receiver_type", candidate.ReceiverType}, {"receiver_origin", candidate.ReceiverOrigin},
		{"receiver_shape", candidate.ReceiverShape}, {"declared_scope", candidate.DeclaredScope},
		{"receiver_chain", candidate.ReceiverChain}, {"import_chain", candidate.ImportChain},
		{"flow_source_stable_ids", mustJSON(candidate.FlowSourceStableIDs)}, {"flow_edge_stable_ids", mustJSON(candidate.FlowEdgeStableIDs)},
		{"flow_source_facts", mustJSON(candidate.FlowSourceFacts)}, {"flow_edge_facts", mustJSON(candidate.FlowEdgeFacts)},
		{"target_stable_id", candidate.TargetStableID}, {"verification_outcome", candidate.VerificationStatus},
	}
	for _, fact := range facts {
		if fact.value == "" || fact.value == "[]" || fact.value == "null" {
			continue
		}
		steps = append(steps, ResolutionDerivationStep{StepID: fmt.Sprintf("%02d:%s", len(steps)+1, fact.kind), Kind: fact.kind, Value: fact.value})
	}
	return steps
}

func validateCandidateDerivation(kind string, candidate *ResolutionCandidate) error {
	if candidate.TargetStableID == "" || candidate.TargetNativeID == "" || candidate.Mechanism == "" {
		return fmt.Errorf("candidate derivation requires stable target identities and mechanism")
	}
	switch kind {
	case "declared_type":
		if candidate.ReceiverType == "" || candidate.ReceiverOrigin == "" {
			return fmt.Errorf("declared_type requires receiver type and origin")
		}
	case "import_binding":
		if candidate.ImportChain == "" || candidate.ImportChain == "[]" {
			return fmt.Errorf("import_binding requires an import chain")
		}
	case "single_implementation", "interface_implementors":
		if candidate.DeclaredScope == "" {
			return fmt.Errorf("%s requires implementation scope", kind)
		}
	case "variable_type_flow":
		if len(candidate.FlowSourceStableIDs) == 0 && len(candidate.FlowEdgeStableIDs) == 0 {
			return fmt.Errorf("variable_type_flow requires typed source or propagation facts")
		}
		for _, id := range candidate.FlowSourceStableIDs {
			if err := validateVTAFactID(id, "vta_source_"); err != nil {
				return err
			}
		}
		for _, id := range candidate.FlowEdgeStableIDs {
			if err := validateVTAFactID(id, "vta_edge_"); err != nil {
				return err
			}
		}
	}
	return nil
}

func validateVTAFactID(id, prefix string) error {
	if !strings.HasPrefix(id, prefix) || len(id) <= len(prefix) {
		return fmt.Errorf("variable_type_flow contains unbound fact ID %q", id)
	}
	return nil
}

var receiverOriginVocabulary = map[string]struct{}{
	"": {}, "import": {}, "field_annotation": {}, "param_annotation": {},
	"assignment": {}, "return_type": {}, "call_syntax": {}, "type_qualifier": {},
}

type GraphCompletionIdentity struct {
	Schema                    string `json:"schema"`
	RepositoryRevision        string `json:"repository_revision"`
	BuildInfoSchema           string `json:"build_info_schema"`
	BuildID                   string `json:"build_id"`
	GitCommit                 string `json:"git_commit"`
	BuildTimeUTC              string `json:"build_time_utc"`
	SourceFingerprint         string `json:"source_fingerprint"`
	ExecutableSHA256          string `json:"executable_sha256"`
	GoToolchain               string `json:"go_toolchain"`
	BuildTags                 string `json:"build_tags"`
	GraphSchemaVersion        string `json:"graph_schema_version"`
	ResolutionContract        string `json:"resolution_contract"`
	ResolutionAuthoritySchema string `json:"resolution_authority_schema"`
	Complete                  bool   `json:"complete"`
}

func (g GraphCompletionIdentity) receipt() (string, string, error) {
	if g.Schema != GraphCompletionSchema || !g.Complete || g.RepositoryRevision == "" ||
		g.BuildInfoSchema == "" || g.BuildID == "" || g.GitCommit == "" ||
		g.BuildTimeUTC == "" || g.SourceFingerprint == "" || g.ExecutableSHA256 == "" ||
		g.GoToolchain == "" || g.BuildTags == "" || g.GraphSchemaVersion == "" ||
		g.ResolutionContract == "" || g.ResolutionAuthoritySchema == "" {
		return "", "", fmt.Errorf("incomplete graph completion identity")
	}
	payload, err := json.Marshal(g)
	if err != nil {
		return "", "", err
	}
	sum := sha256.Sum256(payload)
	return string(payload), hex.EncodeToString(sum[:]), nil
}

func BindGraphCompletionTx(tx *sql.Tx, identity GraphCompletionIdentity) error {
	payload, receiptSHA, err := identity.receipt()
	if err != nil {
		return err
	}
	values := map[string]string{
		"graph_resolution_schema_version":   strconv.Itoa(CallResolutionSchemaVersion),
		"graph_resolution_revision":         identity.RepositoryRevision,
		"graph_resolution_complete":         "1",
		"graph_completion_schema":           identity.Schema,
		"graph_completion_receipt":          payload,
		"graph_completion_receipt_sha256":   receiptSHA,
		"graph_producer_build_id":           identity.BuildID,
		"graph_producer_source_fingerprint": identity.SourceFingerprint,
		"graph_producer_executable_sha256":  identity.ExecutableSHA256,
		"graph_producer_git_commit":         identity.GitCommit,
		"graph_producer_build_time_utc":     identity.BuildTimeUTC,
		"graph_producer_go_toolchain":       identity.GoToolchain,
		"graph_producer_build_tags":         identity.BuildTags,
		"graph_resolution_authority_schema": identity.ResolutionAuthoritySchema,
	}
	for key, value := range values {
		if _, err := tx.Exec(`INSERT INTO project_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value`, key, value); err != nil {
			return fmt.Errorf("bind graph completion %s: %w", key, err)
		}
	}
	return nil
}

// Closure is one row of the transitive-reachability sidecar (C7 / RF-4).
// SourceID transitively reaches TargetID in Depth hops; MinConfidence is the
// weakest edge confidence along that path (the path is built over VERIFIED
// edges only — see internal/closure).
type Closure struct {
	SourceID      int64
	TargetID      int64
	Depth         int
	MinConfidence float64
}

// Property represents a structural fact about a code node (guard clause, return shape, etc.)
type Property struct {
	ID         int64
	NodeID     int64
	Kind       string // guard_clause, return_shape, exception_type, docstring, caller_usage, conditional_return, side_effect, param, security_tag, exception_flow, exception_handler, fingerprint, field_read, boundary_condition, class_field, class_decorator
	Value      string
	Line       int
	Confidence float64
}

// Assertion represents an assertion extracted from a test function.
type Assertion struct {
	ID              int64
	TestNodeID      int64
	TargetNodeID    int64   // 0 if unresolved
	ResolutionScore float64 // multi-signal score that produced the link
	Kind            string  // assertEqual, assertRaises, expect, assert, assert_eq, etc.
	Expression      string  // readable assertion expression
	Expected        string  // expected value if extractable
	Line            int
}

// Open creates or opens an SQLite graph database.
//
// RC-04: synchronous=NORMAL (not OFF) is the minimum safe setting for WAL —
// guarantees durability across power loss / OOM / SIGKILL after a successful
// Commit, while still avoiding the per-transaction fsync of FULL. OFF was
// silently corrupting the WAL when the indexer was killed mid-write.
func Open(path string) (*DB, error) {
	db, err := sql.Open("sqlite3", path+"?_journal_mode=WAL&_synchronous=NORMAL&_busy_timeout=5000")
	if err != nil {
		return nil, fmt.Errorf("open db: %w", err)
	}
	if err := createSchema(db); err != nil {
		db.Close()
		return nil, fmt.Errorf("create schema: %w", err)
	}
	return &DB{db: db}, nil
}

// Close closes the database.
func (d *DB) Close() error { return d.db.Close() }

// ValidateForeignKeys checks all FK constraints after data is fully loaded.
// Called post-insert (not during) because batch inserts may reference
// parent nodes not yet inserted at the time of the child INSERT.
func (d *DB) ValidateForeignKeys() error {
	rows, err := d.db.Query("PRAGMA foreign_key_check")
	if err != nil {
		return fmt.Errorf("fk check: %w", err)
	}
	defer rows.Close()
	violations := 0
	for rows.Next() {
		violations++
	}
	if violations > 0 {
		return fmt.Errorf("%d foreign key violations found in graph.db", violations)
	}
	return nil
}

// CheckpointWAL forces a TRUNCATE checkpoint so all WAL frames are folded
// into the main database file and the WAL is reset. Called after each
// transaction commit during incremental reindex; bounds reader-vs-writer
// torn-read windows and shrinks the WAL footprint on shared volumes.
//
// RC-04: failure here is non-fatal (logged) — the data has been Commit'd, the
// checkpoint is purely a hygiene step.
func (d *DB) CheckpointWAL() {
	if err := d.CheckpointWALRequired(); err != nil {
		log.Printf("WARNING: wal_checkpoint(TRUNCATE) failed: %v", err)
	}
}

// CheckpointWALRequired is the publication-boundary variant. A full staged
// graph must not be made authoritative unless every committed frame is folded
// into the single content artifact that will be atomically renamed.
func (d *DB) CheckpointWALRequired() error {
	if _, err := d.db.Exec("PRAGMA wal_checkpoint(TRUNCATE)"); err != nil {
		return err
	}
	return nil
}

func createSchema(db *sql.DB) error {
	schema := `
	CREATE TABLE IF NOT EXISTS nodes (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		label TEXT NOT NULL,
		name TEXT NOT NULL,
		qualified_name TEXT,
		file_path TEXT NOT NULL,
		start_line INTEGER,
		end_line INTEGER,
		signature TEXT,
		return_type TEXT,
		is_exported BOOLEAN DEFAULT 0,
		is_test BOOLEAN DEFAULT 0,
		language TEXT NOT NULL,
		parent_id INTEGER REFERENCES nodes(id),
		stable_id TEXT,
		node_type TEXT,
		schema_version INTEGER,
		repo_id TEXT,
		source_revision TEXT,
		producer_build_id TEXT,
		file_node_id INTEGER,
		caller_symbol_id TEXT,
		ast_path TEXT,
		byte_start INTEGER,
		byte_end INTEGER,
		line_start INTEGER,
		column_start INTEGER,
		dispatch_form TEXT,
		callee_lexeme TEXT,
		declared_receiver_type_id TEXT,
		receiver_value_id TEXT,
		argument_arity INTEGER,
		parse_state TEXT,
		candidate_state TEXT,
		selected_target_id TEXT,
		candidate_count_v2 INTEGER,
		derivation_set_id TEXT,
		completeness_set_id TEXT,
		callsite_id TEXT,
		target_symbol_id TEXT,
		pass_kind TEXT,
		pass_version TEXT,
		step_ordinal INTEGER,
		operation TEXT,
		input_fact_ids TEXT,
		declared_type_id TEXT,
		allocated_type_id TEXT,
		field_id TEXT,
		allocation_site_id TEXT,
		scope_id TEXT,
		import_id TEXT,
		configuration_artifact_id TEXT,
		boundary_kind TEXT,
		boundary_id TEXT,
		fact_status TEXT,
		covered_units INTEGER,
		known_units INTEGER,
		reason_code TEXT,
		blocking_pass_kinds TEXT,
		candidate_stable_ids TEXT,
		flow_type_stable_ids TEXT,
		allocation_type_stable_ids TEXT,
		reachable_stable_ids TEXT,
		root_stable_ids TEXT,
		root_policy TEXT,
		root_policy_version TEXT,
		root_completeness TEXT,
		root_boundary TEXT,
		policy_name TEXT,
		semantic_version TEXT,
		policy_json TEXT,
		policy_hash TEXT,
		created_at TEXT,
		created_by TEXT,
		fact_schema_min INTEGER,
		fact_schema_max INTEGER,
		explanation_template_version TEXT,
		active_from TEXT,
		supersedes_id TEXT
	);

	CREATE TABLE IF NOT EXISTS edges (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		source_id INTEGER NOT NULL REFERENCES nodes(id),
		target_id INTEGER NOT NULL REFERENCES nodes(id),
		type TEXT NOT NULL,
		source_line INTEGER,
		source_file TEXT,
		resolution_method TEXT,
		confidence REAL DEFAULT 0.0,
		metadata TEXT,
		trust_tier TEXT DEFAULT 'SPECULATIVE',
		candidate_count INTEGER DEFAULT 1,
		evidence_type TEXT,
		verification_status TEXT DEFAULT 'unverified',
		derivation_contract TEXT,
		derivation_kind TEXT,
		evidence_set TEXT,
		analysis_boundary TEXT,
		pass_kind TEXT,
		pass_version TEXT,
		pass_status TEXT,
		abstention_reason TEXT,
		sibling_count INTEGER,
		producer_build_id TEXT,
		producer_source_fingerprint TEXT,
		pass_coverage TEXT,
		provenance_steps TEXT,
		declared_scope TEXT,
		receiver_type TEXT,
		receiver_origin TEXT,
		receiver_shape TEXT,
		receiver_chain TEXT,
		import_chain TEXT,
		export_status TEXT,
		parser_complete BOOLEAN,
		stable_id TEXT,
		schema_version INTEGER,
		callsite_stable_id TEXT,
		target_symbol_id TEXT,
		ordinal INTEGER,
		viability TEXT,
		derivation_fact_ids TEXT,
		exclusion_fact_ids TEXT,
		selection_rule_id TEXT
	);

	CREATE TABLE IF NOT EXISTS resolution_symbols (
		stable_id TEXT PRIMARY KEY,
		native_id TEXT NOT NULL UNIQUE,
		native_kind TEXT NOT NULL,
		normalized_kind TEXT NOT NULL,
		language TEXT NOT NULL,
		path TEXT NOT NULL,
		qualified_name TEXT NOT NULL,
		start_line INTEGER NOT NULL,
		end_line INTEGER NOT NULL,
		export_status TEXT NOT NULL
	);

	CREATE TABLE IF NOT EXISTS resolution_callsites (
		callsite_id TEXT PRIMARY KEY,
		callsite_ordinal INTEGER NOT NULL UNIQUE,
		repository_revision TEXT NOT NULL,
		source_stable_id TEXT NOT NULL REFERENCES resolution_symbols(stable_id),
		source_native_id TEXT NOT NULL,
		source_id INTEGER,
		source_line INTEGER,
		source_file TEXT NOT NULL,
		callee TEXT NOT NULL,
		language TEXT NOT NULL,
		dispatch_state TEXT NOT NULL,
		candidate_count INTEGER NOT NULL DEFAULT 0,
		selected_target_stable_id TEXT,
		selected_target_native_id TEXT,
		mechanism TEXT NOT NULL,
		verification_status TEXT NOT NULL DEFAULT 'unverified'
	);

	CREATE TABLE IF NOT EXISTS resolution_candidates (
		callsite_id TEXT NOT NULL REFERENCES resolution_callsites(callsite_id) ON DELETE CASCADE,
		target_id INTEGER NOT NULL REFERENCES nodes(id),
		target_stable_id TEXT NOT NULL REFERENCES resolution_symbols(stable_id),
		target_native_id TEXT NOT NULL,
		ordinal INTEGER NOT NULL,
		mechanism TEXT NOT NULL,
		declared_scope TEXT NOT NULL DEFAULT '',
		receiver_type TEXT NOT NULL DEFAULT '',
		receiver_origin TEXT NOT NULL DEFAULT '',
		receiver_shape TEXT NOT NULL DEFAULT '',
		receiver_chain TEXT NOT NULL DEFAULT '[]',
		import_chain TEXT NOT NULL DEFAULT '[]',
		dynamic_dispatch BOOLEAN NOT NULL DEFAULT 0,
		export_status TEXT NOT NULL DEFAULT 'unknown',
		parser_complete BOOLEAN,
		verification_status TEXT NOT NULL,
		selected BOOLEAN NOT NULL DEFAULT 0,
		PRIMARY KEY(callsite_id, ordinal),
		UNIQUE(callsite_id, target_stable_id)
	);
	CREATE INDEX IF NOT EXISTS idx_resolution_callsites_source ON resolution_callsites(source_id);
	CREATE INDEX IF NOT EXISTS idx_resolution_candidates_target ON resolution_candidates(target_id);

	CREATE TABLE IF NOT EXISTS file_hashes (
		file_path TEXT PRIMARY KEY,
		content_hash TEXT NOT NULL,
		language TEXT,
		indexed_at TEXT NOT NULL
	);

	CREATE TABLE IF NOT EXISTS project_meta (
		key TEXT PRIMARY KEY,
		value TEXT
	);

	CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
	CREATE INDEX IF NOT EXISTS idx_nodes_qname ON nodes(qualified_name);
	CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);
	CREATE INDEX IF NOT EXISTS idx_nodes_label ON nodes(label);
	CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);
	CREATE INDEX IF NOT EXISTS idx_nodes_test ON nodes(is_test) WHERE is_test = 1;
	CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
	CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
	CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type);
	CREATE INDEX IF NOT EXISTS idx_edges_source_type ON edges(source_id, type);
	CREATE INDEX IF NOT EXISTS idx_edges_target_type ON edges(target_id, type);
	CREATE INDEX IF NOT EXISTS idx_edges_resolution ON edges(resolution_method);
	CREATE INDEX IF NOT EXISTS idx_edges_confidence ON edges(confidence);
	CREATE INDEX IF NOT EXISTS idx_edges_trust_tier ON edges(trust_tier);
	CREATE INDEX IF NOT EXISTS idx_edges_target_tier ON edges(target_id, trust_tier);
	CREATE INDEX IF NOT EXISTS idx_edges_source_file ON edges(source_file);

	CREATE TABLE IF NOT EXISTS properties (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		node_id INTEGER NOT NULL REFERENCES nodes(id),
		kind TEXT NOT NULL,
		value TEXT NOT NULL,
		line INTEGER,
		confidence REAL DEFAULT 1.0
	);

	CREATE TABLE IF NOT EXISTS assertions (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		test_node_id INTEGER NOT NULL REFERENCES nodes(id),
		target_node_id INTEGER DEFAULT 0,
		resolution_score REAL DEFAULT 0.0,
		kind TEXT NOT NULL,
		expression TEXT NOT NULL,
		expected TEXT,
		line INTEGER
	);

	CREATE INDEX IF NOT EXISTS idx_properties_node ON properties(node_id);
	CREATE INDEX IF NOT EXISTS idx_properties_kind ON properties(kind);
	CREATE INDEX IF NOT EXISTS idx_properties_node_kind ON properties(node_id, kind);
	CREATE INDEX IF NOT EXISTS idx_assertions_test ON assertions(test_node_id);
	CREATE INDEX IF NOT EXISTS idx_assertions_target ON assertions(target_node_id);

	CREATE TABLE IF NOT EXISTS cochanges (
		file_a TEXT NOT NULL,
		file_b TEXT NOT NULL,
		count INTEGER NOT NULL DEFAULT 1,
		PRIMARY KEY(file_a, file_b)
	);
	CREATE INDEX IF NOT EXISTS idx_cochanges_a ON cochanges(file_a);
	CREATE INDEX IF NOT EXISTS idx_cochanges_b ON cochanges(file_b);

	-- C7 (RF-4): transitive-closure sidecar over VERIFIED edges only.
	-- A row (source_id, target_id, depth, min_confidence) means source_id
	-- transitively reaches target_id in depth hops, where min_confidence is
	-- the weakest edge confidence along that path. Built offline by the
	-- closure package after CALLS resolution; read by impact/trace via an
	-- indexed SELECT (depth<=3, min_confidence>=0.5). Absence of this table on
	-- an old graph.db triggers the Python live-BFS fallback (zero regression).
	CREATE TABLE IF NOT EXISTS closure (
		source_id INTEGER,
		target_id INTEGER,
		depth INTEGER,
		min_confidence REAL,
		PRIMARY KEY(source_id, target_id, depth)
	);
	CREATE INDEX IF NOT EXISTS idx_closure_source ON closure(source_id);
	CREATE INDEX IF NOT EXISTS idx_closure_target ON closure(target_id);

	`
	_, err := db.Exec(schema)
	if err != nil {
		return err
	}
	// Existing graphs may predate typed call-candidate facts. Incremental opens
	// must add the nullable columns before they can fail closed and invalidate the
	// old resolution overlay. Full builds create the columns above directly.
	typedColumns := []struct{ name, definition string }{
		{"derivation_contract", "TEXT"}, {"derivation_kind", "TEXT"}, {"evidence_set", "TEXT"},
		{"analysis_boundary", "TEXT"}, {"pass_kind", "TEXT"}, {"pass_version", "TEXT"},
		{"pass_status", "TEXT"}, {"abstention_reason", "TEXT"}, {"sibling_count", "INTEGER"},
		{"producer_build_id", "TEXT"}, {"producer_source_fingerprint", "TEXT"},
		{"pass_coverage", "TEXT"}, {"provenance_steps", "TEXT"},
		{"declared_scope", "TEXT"}, {"receiver_type", "TEXT"}, {"receiver_origin", "TEXT"},
		{"receiver_shape", "TEXT"}, {"receiver_chain", "TEXT"}, {"import_chain", "TEXT"},
		{"export_status", "TEXT"}, {"parser_complete", "BOOLEAN"},
		{"stable_id", "TEXT"}, {"schema_version", "INTEGER"}, {"callsite_stable_id", "TEXT"},
		{"target_symbol_id", "TEXT"}, {"ordinal", "INTEGER"}, {"viability", "TEXT"},
		{"derivation_fact_ids", "TEXT"}, {"exclusion_fact_ids", "TEXT"}, {"selection_rule_id", "TEXT"},
	}
	for _, column := range typedColumns {
		var count int
		if err := db.QueryRow(`SELECT count(*) FROM pragma_table_info('edges') WHERE name=?`, column.name).Scan(&count); err != nil {
			return fmt.Errorf("inspect edges.%s: %w", column.name, err)
		}
		if count == 0 {
			if _, err := db.Exec(`ALTER TABLE edges ADD COLUMN ` + column.name + ` ` + column.definition); err != nil {
				return fmt.Errorf("add edges.%s: %w", column.name, err)
			}
		}
	}
	nodeColumns := []struct{ name, definition string }{
		{"stable_id", "TEXT"}, {"node_type", "TEXT"}, {"schema_version", "INTEGER"},
		{"repo_id", "TEXT"}, {"source_revision", "TEXT"}, {"producer_build_id", "TEXT"},
		{"file_node_id", "INTEGER"}, {"caller_symbol_id", "TEXT"}, {"ast_path", "TEXT"},
		{"byte_start", "INTEGER"}, {"byte_end", "INTEGER"}, {"line_start", "INTEGER"},
		{"column_start", "INTEGER"}, {"dispatch_form", "TEXT"}, {"callee_lexeme", "TEXT"},
		{"declared_receiver_type_id", "TEXT"}, {"receiver_value_id", "TEXT"}, {"argument_arity", "INTEGER"},
		{"parse_state", "TEXT"}, {"candidate_state", "TEXT"}, {"selected_target_id", "TEXT"},
		{"candidate_count_v2", "INTEGER"}, {"derivation_set_id", "TEXT"}, {"completeness_set_id", "TEXT"},
		{"callsite_id", "TEXT"}, {"target_symbol_id", "TEXT"}, {"pass_kind", "TEXT"},
		{"pass_version", "TEXT"}, {"step_ordinal", "INTEGER"}, {"operation", "TEXT"},
		{"input_fact_ids", "TEXT"}, {"declared_type_id", "TEXT"}, {"allocated_type_id", "TEXT"},
		{"field_id", "TEXT"}, {"allocation_site_id", "TEXT"}, {"scope_id", "TEXT"},
		{"import_id", "TEXT"}, {"configuration_artifact_id", "TEXT"},
		{"boundary_kind", "TEXT"}, {"boundary_id", "TEXT"},
		{"fact_status", "TEXT"}, {"covered_units", "INTEGER"}, {"known_units", "INTEGER"},
		{"reason_code", "TEXT"}, {"blocking_pass_kinds", "TEXT"}, {"policy_name", "TEXT"},
		{"candidate_stable_ids", "TEXT"}, {"flow_type_stable_ids", "TEXT"}, {"allocation_type_stable_ids", "TEXT"},
		{"reachable_stable_ids", "TEXT"},
		{"root_stable_ids", "TEXT"},
		{"root_policy", "TEXT"},
		{"root_policy_version", "TEXT"}, {"root_completeness", "TEXT"}, {"root_boundary", "TEXT"},
		{"semantic_version", "TEXT"}, {"policy_json", "TEXT"}, {"policy_hash", "TEXT"},
		{"created_at", "TEXT"}, {"created_by", "TEXT"}, {"fact_schema_min", "INTEGER"},
		{"fact_schema_max", "INTEGER"}, {"explanation_template_version", "TEXT"},
		{"active_from", "TEXT"}, {"supersedes_id", "TEXT"},
	}
	for _, column := range nodeColumns {
		var count int
		if err := db.QueryRow(`SELECT count(*) FROM pragma_table_info('nodes') WHERE name=?`, column.name).Scan(&count); err != nil {
			return fmt.Errorf("inspect nodes.%s: %w", column.name, err)
		}
		if count == 0 {
			if _, err := db.Exec(`ALTER TABLE nodes ADD COLUMN ` + column.name + ` ` + column.definition); err != nil {
				return fmt.Errorf("add nodes.%s: %w", column.name, err)
			}
		}
	}
	if _, err := db.Exec(`CREATE INDEX IF NOT EXISTS idx_edges_candidate_policy ON edges(type, derivation_kind, evidence_set, candidate_count, target_id)`); err != nil {
		return fmt.Errorf("create candidate policy index: %w", err)
	}
	// Old v2 producers could publish a second generic CALLS row for the same
	// source/target endpoint. Coalesce those rows and install the invariant in
	// one transaction so a failed migration leaves the prior database intact.
	migration, err := db.Begin()
	if err != nil {
		return fmt.Errorf("begin CALLS uniqueness migration: %w", err)
	}
	if _, err := migration.Exec(`DELETE FROM edges WHERE type='CALLS' AND id NOT IN
		(SELECT MIN(id) FROM edges WHERE type='CALLS' GROUP BY source_id,target_id,type)`); err != nil {
		_ = migration.Rollback()
		return fmt.Errorf("coalesce duplicate CALLS endpoints: %w", err)
	}
	if _, err := migration.Exec(`CREATE UNIQUE INDEX IF NOT EXISTS idx_calls_unique_endpoint
		ON edges(source_id,target_id,type) WHERE type='CALLS'`); err != nil {
		_ = migration.Rollback()
		return fmt.Errorf("create CALLS endpoint invariant: %w", err)
	}
	if err := migration.Commit(); err != nil {
		return fmt.Errorf("commit CALLS uniqueness migration: %w", err)
	}
	indexStatements := []string{
		`CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_stable_id ON nodes(stable_id) WHERE stable_id IS NOT NULL`,
		`CREATE INDEX IF NOT EXISTS idx_callsite_location_v2 ON nodes(repo_id,source_revision,file_node_id,byte_start) WHERE node_type='callsite'`,
		`CREATE INDEX IF NOT EXISTS idx_callsite_caller_state_v2 ON nodes(caller_symbol_id,candidate_state) WHERE node_type='callsite'`,
		`CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_stable_id_v2 ON edges(stable_id) WHERE stable_id IS NOT NULL`,
		`CREATE INDEX IF NOT EXISTS idx_candidate_callsite_v2 ON edges(callsite_stable_id,viability,target_symbol_id) WHERE type='CANDIDATE_TARGET'`,
		`CREATE INDEX IF NOT EXISTS idx_candidate_reverse_v2 ON edges(target_symbol_id,callsite_stable_id) WHERE type='CANDIDATE_TARGET'`,
		`CREATE INDEX IF NOT EXISTS idx_derivation_fact_v2 ON nodes(callsite_id,target_symbol_id,pass_kind,step_ordinal) WHERE node_type='derivation_fact'`,
		`CREATE INDEX IF NOT EXISTS idx_completeness_fact_v2 ON nodes(callsite_id,pass_kind,fact_status) WHERE node_type='completeness_fact'`,
		`CREATE UNIQUE INDEX IF NOT EXISTS idx_policy_version_v2 ON nodes(policy_name,semantic_version) WHERE node_type='query_policy_version'`,
	}
	for _, statement := range indexStatements {
		if _, err := db.Exec(statement); err != nil {
			return fmt.Errorf("create v2 resolution index: %w", err)
		}
	}

	// FTS5 virtual table: SEPARATE from main schema because some SQLite builds
	// (e.g. container images) lack FTS5 support. Non-fatal: if FTS5 is unavailable,
	// the Python localizer falls back to name-match-only seeding.
	_, ftsErr := db.Exec(`
		CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
			name, qualified_name, signature, file_path,
			content='nodes', content_rowid='id'
		);
	`)
	if ftsErr != nil {
		log.Printf("[WARN] FTS5 not available (non-fatal): %v", ftsErr)
	}
	return nil
}

// PopulateFTS5 fills the nodes_fts virtual table from the nodes table.
// Must be called AFTER all nodes are inserted (end of DEFINITIONS pass).
// Idempotent: DELETEs existing FTS5 content first, then re-inserts.
// Non-fatal: if FTS5 table doesn't exist (SQLite build without FTS5),
// logs a warning and returns nil. The Python localizer handles this
// gracefully with name-match-only fallback.
func (d *DB) PopulateFTS5() error {
	// Check if nodes_fts table exists (FTS5 may not be compiled in)
	var count int
	err := d.db.QueryRow("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='nodes_fts'").Scan(&count)
	if err != nil || count == 0 {
		log.Printf("[WARN] nodes_fts table not found (FTS5 unavailable); skipping FTS5 population")
		return nil
	}
	insertSQL := `INSERT INTO nodes_fts(rowid, name, qualified_name, signature, file_path)
		 SELECT id, name, COALESCE(qualified_name, ''), COALESCE(signature, ''), file_path
		 FROM nodes`
	// Clear any existing content (idempotent rebuild), then re-insert.
	_, delErr := d.db.Exec("DELETE FROM nodes_fts")
	if delErr == nil {
		if _, err := d.db.Exec(insertSQL); err == nil {
			return nil
		} else {
			delErr = err // fall through to the DROP+recreate recovery
		}
	}
	// Recovery: the external-content FTS5 index can be CORRUPT ("database disk image
	// is malformed") — e.g. when nodes was UPDATEd (LSP resolve) or graph.db was
	// `docker cp`'d away from its -wal between containers, stranding the index. DELETE
	// then fails. DROP the vtable and recreate it fresh from the current nodes table.
	log.Printf("[WARN] nodes_fts clear/insert failed (%v) — DROP+recreate to self-heal the FTS5 index", delErr)
	if _, err := d.db.Exec("DROP TABLE IF EXISTS nodes_fts"); err != nil {
		return fmt.Errorf("drop corrupt nodes_fts: %w", err)
	}
	if _, err := d.db.Exec(`CREATE VIRTUAL TABLE nodes_fts USING fts5(
		name, qualified_name, signature, file_path,
		content='nodes', content_rowid='id'
	)`); err != nil {
		return fmt.Errorf("recreate nodes_fts: %w", err)
	}
	if _, err := d.db.Exec(insertSQL); err != nil {
		return fmt.Errorf("populate nodes_fts after recreate: %w", err)
	}
	return nil
}

// FTS5RowCount returns the number of rows in nodes_fts, or -1 when the table
// is absent (the binary was built WITHOUT the `sqlite_fts5` build tag, so the
// CREATE VIRTUAL TABLE … fts5 was rejected). Used by the GT_REQUIRE_FTS5
// preflight gate in main to abort a paid run that would silently degrade to the
// Python name-match fallback. A real, queryable FTS5 index is the first stage of
// the localizer pipeline; we never want to pay for a run without it.
func (d *DB) FTS5RowCount() int {
	var n int
	if err := d.db.QueryRow("SELECT COUNT(*) FROM nodes_fts").Scan(&n); err != nil {
		return -1
	}
	return n
}

// InsertNode inserts a node and returns its ID.
func (d *DB) InsertNode(n *Node) (int64, error) {
	res, err := d.db.Exec(
		`INSERT INTO nodes (label, name, qualified_name, file_path, start_line, end_line,
		 signature, return_type, is_exported, is_test, language, parent_id)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		n.Label, n.Name, n.QualifiedName, n.FilePath, n.StartLine, n.EndLine,
		n.Signature, n.ReturnType, n.IsExported, n.IsTest, n.Language, nullableParentID(n.ParentID),
	)
	if err != nil {
		return 0, err
	}
	return res.LastInsertId()
}

// InsertEdge inserts an edge.
func (d *DB) InsertEdge(e *Edge) error {
	_, err := d.db.Exec(
		`INSERT INTO edges (source_id, target_id, type, source_line, source_file, resolution_method, confidence, metadata,
		 trust_tier, candidate_count, evidence_type, verification_status)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		e.SourceID, e.TargetID, e.Type, e.SourceLine, e.SourceFile, e.ResolutionMethod, e.Confidence, e.Metadata,
		e.TrustTier, e.CandidateCount, e.EvidenceType, e.VerificationStatus,
	)
	return err
}

// InsertFileHash records a file's content hash for incremental reindexing.
//
// RC-17 (F-004): the “indexed_at“ column is wall-clock by default, which
// makes “graph.db“ non-byte-deterministic across two builds. When
// “GT_INDEX_FIXED_TS“ is set in the environment we use that literal
// value instead — enables the deterministic-build CI test (build twice,
// assert byte equality on every column except whatever the test
// explicitly excludes). Format expectation: RFC3339 UTC (caller's
// responsibility; we copy verbatim).
func (d *DB) InsertFileHash(filePath, hash, language string) error {
	ts := os.Getenv("GT_INDEX_FIXED_TS")
	if ts == "" {
		ts = time.Now().UTC().Format(time.RFC3339)
	}
	_, err := d.db.Exec(
		`INSERT OR REPLACE INTO file_hashes (file_path, content_hash, language, indexed_at) VALUES (?, ?, ?, ?)`,
		filePath, hash, language, ts,
	)
	return err
}

// SetMeta stores a key-value pair in project_meta.
func (d *DB) SetMeta(key, value string) error {
	_, err := d.db.Exec(`INSERT OR REPLACE INTO project_meta (key, value) VALUES (?, ?)`, key, value)
	return err
}

// GetFileHash returns the stored hash for a file, or empty string if not found.
func (d *DB) GetFileHash(filePath string) string {
	var hash string
	d.db.QueryRow(`SELECT content_hash FROM file_hashes WHERE file_path = ?`, filePath).Scan(&hash)
	return hash
}

// BeginTx starts a transaction for batch inserts.
func (d *DB) BeginTx() (*sql.Tx, error) { return d.db.Begin() }

// BatchInsertNodes inserts nodes in a single transaction with a prepared statement.
// Returns the auto-generated IDs in the same order as input.
func (d *DB) BatchInsertNodes(nodes []*Node) ([]int64, error) {
	tx, err := d.db.Begin()
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	stmt, err := tx.Prepare(
		`INSERT INTO nodes (label, name, qualified_name, file_path, start_line, end_line,
		 signature, return_type, is_exported, is_test, language, parent_id)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
	)
	if err != nil {
		tx.Rollback()
		return nil, fmt.Errorf("prepare: %w", err)
	}
	defer stmt.Close()

	ids := make([]int64, len(nodes))
	for i, n := range nodes {
		res, err := stmt.Exec(
			n.Label, n.Name, n.QualifiedName, n.FilePath, n.StartLine, n.EndLine,
			n.Signature, n.ReturnType, n.IsExported, n.IsTest, n.Language, nullableParentID(n.ParentID),
		)
		if err != nil {
			tx.Rollback()
			return nil, fmt.Errorf("insert node %d: %w", i, err)
		}
		id, err := res.LastInsertId()
		if err != nil {
			tx.Rollback()
			return nil, fmt.Errorf("last insert id for node %d: %w", i, err)
		}
		ids[i] = id
	}
	if err := tx.Commit(); err != nil {
		return nil, fmt.Errorf("commit: %w", err)
	}
	return ids, nil
}

// BatchInsertEdges inserts edges in a single transaction with a prepared statement.
func (d *DB) BatchInsertEdges(edges []*Edge) error {
	if len(edges) == 0 {
		return nil
	}
	tx, err := d.db.Begin()
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	stmt, err := tx.Prepare(
		`INSERT INTO edges (source_id, target_id, type, source_line, source_file,
		 resolution_method, confidence, metadata, trust_tier, candidate_count, evidence_type, verification_status)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
	)
	if err != nil {
		tx.Rollback()
		return fmt.Errorf("prepare: %w", err)
	}
	defer stmt.Close()

	for i, e := range edges {
		_, err := stmt.Exec(
			e.SourceID, e.TargetID, e.Type, e.SourceLine, e.SourceFile,
			e.ResolutionMethod, e.Confidence, e.Metadata,
			e.TrustTier, e.CandidateCount, e.EvidenceType, e.VerificationStatus,
		)
		if err != nil {
			tx.Rollback()
			return fmt.Errorf("insert edge %d: %w", i, err)
		}
	}
	return tx.Commit()
}

func (d *DB) BatchInsertResolutionSymbols(symbols []*ResolutionSymbol) error {
	tx, err := d.db.Begin()
	if err != nil {
		return err
	}
	if err := BatchInsertResolutionSymbolsTx(tx, symbols); err != nil {
		_ = tx.Rollback()
		return err
	}
	return tx.Commit()
}

func (d *DB) BatchInsertResolutionCallsites(callsites []*ResolutionCallsite) error {
	tx, err := d.db.Begin()
	if err != nil {
		return err
	}
	if err := BatchInsertResolutionCallsitesTx(tx, callsites); err != nil {
		_ = tx.Rollback()
		return err
	}
	return tx.Commit()
}

func (d *DB) BatchInsertResolutionCandidates(candidates []*ResolutionCandidate) error {
	tx, err := d.db.Begin()
	if err != nil {
		return err
	}
	if err := BatchInsertResolutionCandidatesTx(tx, candidates); err != nil {
		_ = tx.Rollback()
		return err
	}
	return tx.Commit()
}

// AttachResolutionGraphTx publishes resolver evidence as first-class graph
// objects. Each callsite is a Callsite node linked from its source symbol by a
// HAS_CALLSITE edge; each viable target is linked from that node by a CANDIDATE
// edge. The complete set is written in the caller's transaction so a graph
// reader observes either all attached evidence or none of it.
func AttachResolutionGraphTx(tx *sql.Tx, identity GraphCompletionIdentity, rows []AttachedResolution) error {
	if tx == nil {
		return fmt.Errorf("nil graph transaction")
	}
	if _, _, err := identity.receipt(); err != nil {
		return fmt.Errorf("attach graph with incomplete producer identity: %w", err)
	}
	if err := insertResolutionPolicyV2Tx(tx, ConservativeCandidatePolicy, identity); err != nil {
		return fmt.Errorf("insert conservative query policy: %w", err)
	}
	if err := insertResolutionPolicyV2Tx(tx, InspectAllCandidatePolicy, identity); err != nil {
		return fmt.Errorf("insert inspection query policy: %w", err)
	}
	revision := identity.RepositoryRevision
	for _, row := range rows {
		if row.Callsite == nil || row.Source == nil {
			return fmt.Errorf("graph resolution row missing callsite or source")
		}
		c := row.Callsite
		if c.CandidateCount != len(row.Candidates) {
			return fmt.Errorf("callsite %s candidate count %d != retained rows %d", c.CallsiteID, c.CandidateCount, len(row.Candidates))
		}
		seenOrd := make(map[int]struct{}, len(row.Candidates))
		seenTarget := make(map[int64]struct{}, len(row.Candidates))
		selected := 0
		selectedTarget := ""
		for _, candidate := range row.Candidates {
			if candidate == nil || candidate.TargetID <= 0 {
				return fmt.Errorf("callsite %s has invalid candidate", c.CallsiteID)
			}
			if _, ok := seenOrd[candidate.Ordinal]; ok || candidate.Ordinal != len(seenOrd) {
				return fmt.Errorf("callsite %s candidate ordinals are not dense", c.CallsiteID)
			}
			seenOrd[candidate.Ordinal] = struct{}{}
			if _, ok := seenTarget[candidate.TargetID]; ok {
				return fmt.Errorf("callsite %s retains duplicate candidate target %d", c.CallsiteID, candidate.TargetID)
			}
			seenTarget[candidate.TargetID] = struct{}{}
			if _, ok := receiverOriginVocabulary[candidate.ReceiverOrigin]; !ok && !strings.HasPrefix(candidate.ReceiverOrigin, "vta_flow_stable_ids=") {
				return fmt.Errorf("callsite %s has unknown receiver origin %q", c.CallsiteID, candidate.ReceiverOrigin)
			}
			if candidate.Selected {
				selected++
				selectedTarget = candidate.TargetStableID
			}
		}
		if c.DispatchState == "ambiguous" && selected != 0 {
			return fmt.Errorf("ambiguous callsite %s has selected candidate", c.CallsiteID)
		}
		if c.SelectedTargetStableID != nil {
			if selected != 1 || selectedTarget != *c.SelectedTargetStableID {
				return fmt.Errorf("callsite %s selected target is not exactly one retained candidate", c.CallsiteID)
			}
		} else if selected != 0 {
			return fmt.Errorf("callsite %s has selected row without selected target", c.CallsiteID)
		}
		switch c.DispatchState {
		case "unique":
			if len(row.Candidates) != 1 || selected != 1 {
				return fmt.Errorf("unique callsite %s must retain exactly one selected candidate", c.CallsiteID)
			}
		case "ambiguous":
			if len(row.Candidates) < 2 || selected != 0 {
				return fmt.Errorf("ambiguous callsite %s must retain multiple unselected candidates", c.CallsiteID)
			}
		case "candidate_only":
			if len(row.Candidates) < 1 || selected != 0 {
				return fmt.Errorf("candidate-only callsite %s must retain unselected candidates", c.CallsiteID)
			}
		case "dynamic":
			if len(row.Candidates) != 0 || selected != 0 {
				return fmt.Errorf("dynamic callsite %s must explicitly abstain with zero candidates", c.CallsiteID)
			}
		case "zero", "external_unresolved", "parser_incomplete":
			if len(row.Candidates) != 0 || selected != 0 {
				return fmt.Errorf("unresolved callsite %s cannot retain candidate authority", c.CallsiteID)
			}
		default:
			return fmt.Errorf("callsite %s has unknown dispatch state %q", c.CallsiteID, c.DispatchState)
		}

		derivationKind, evidenceSet, abstentionReason, err := resolutionDerivation(c.Mechanism, c.DispatchState, c.CandidateCount)
		if err != nil {
			return fmt.Errorf("callsite %s: %w", c.CallsiteID, err)
		}
		coverage, steps := resolutionCoverage(c)
		passKind, passStatus := derivationKind, "completed"
		for _, pass := range coverage {
			if pass.Status == "completed_match" || (c.DispatchState == "dynamic" && pass.PassKind == "dynamic_framework") {
				passKind, passStatus = pass.PassKind, pass.Status
				break
			}
		}
		coverageJSON, _ := json.Marshal(coverage)
		stepsJSON, _ := json.Marshal(steps)
		v2, err := prepareResolutionV2(identity, c, row.Candidates)
		if err != nil {
			return fmt.Errorf("prepare callsite %s v2: %w", c.CallsiteID, err)
		}
		callsiteMeta, _ := json.Marshal(map[string]any{
			"callsite_id": c.CallsiteID, "dispatch_state": c.DispatchState,
			"mechanism": c.Mechanism, "repository_revision": revision,
			"verification_status":  c.VerificationStatus,
			"candidate_count":      c.CandidateCount,
			"sibling_count":        c.CandidateCount,
			"derivation_kind":      derivationKind,
			"evidence_set":         evidenceSet,
			"abstention_reason":    abstentionReason,
			"derivation_contract":  ResolutionDerivationContract,
			"structural_authority": "source_syntax", "target_authority": c.VerificationStatus,
		})
		res, err := tx.Exec(`INSERT INTO nodes
			(label,name,qualified_name,file_path,start_line,end_line,signature,language,
			 stable_id,node_type,schema_version,repo_id,source_revision,producer_build_id,file_node_id,caller_symbol_id,
			 ast_path,byte_start,byte_end,line_start,column_start,dispatch_form,callee_lexeme,argument_arity,parse_state,
			 candidate_state,selected_target_id,candidate_count_v2,derivation_set_id,completeness_set_id)
			VALUES ('Callsite',?,?,?,?,?,?,?,?, 'callsite',2,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
			c.Callee, v2.CallsiteID, c.SourceFile, c.SourceLine, c.SourceLine, string(callsiteMeta), c.Language,
			v2.CallsiteID, v2.RepoID, identity.RepositoryRevision, identity.BuildID, v2.FileNodeID, c.CallerSymbolID,
			v2.ASTPath, v2.ByteStart, v2.ByteEnd, c.SourceLine, c.ColumnStart, v2.DispatchForm, c.Callee, c.ArgumentArity,
			v2.ParseState, v2.CandidateState, nullableString(v2.SelectedTargetID), len(v2.Candidates), v2.DerivationSetID, v2.CompletenessSetID)
		if err != nil {
			return fmt.Errorf("insert callsite node %s: %w", c.CallsiteID, err)
		}
		callsiteNodeID, err := res.LastInsertId()
		if err != nil {
			return fmt.Errorf("callsite node id %s: %w", c.CallsiteID, err)
		}
		if err := insertResolutionV2FactsTx(tx, identity, c, callsiteNodeID, v2); err != nil {
			return err
		}
		linkMeta, _ := json.Marshal(map[string]any{
			"callsite_id": c.CallsiteID, "repository_revision": revision,
			"dispatch_state": c.DispatchState, "structural_authority": "source_syntax",
			"target_authority": c.VerificationStatus,
		})
		if _, err := tx.Exec(`INSERT INTO edges
			(source_id,target_id,type,source_line,source_file,resolution_method,confidence,metadata,trust_tier,candidate_count,evidence_type,verification_status,stable_id,schema_version,callsite_stable_id,
			 derivation_contract,derivation_kind,evidence_set,analysis_boundary,pass_kind,pass_version,pass_status,abstention_reason,sibling_count,
			 producer_build_id,producer_source_fingerprint,pass_coverage,provenance_steps)
			VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
			c.SourceID, callsiteNodeID, "HAS_CALLSITE", c.SourceLine, c.SourceFile,
			"graph_native", nil, string(linkMeta), "STRUCTURAL", c.CandidateCount,
			"resolver_callsite", "structural_only", canonicalResolutionID(c.CallerSymbolID, "HAS_CALLSITE", v2.CallsiteID), CallResolutionSchemaVersion, v2.CallsiteID,
			ResolutionDerivationContract, derivationKind, evidenceSet,
			revision, passKind, "1", passStatus, abstentionReason, c.CandidateCount,
			identity.BuildID, identity.SourceFingerprint, string(coverageJSON), string(stepsJSON)); err != nil {
			return fmt.Errorf("link callsite %s: %w", c.CallsiteID, err)
		}
		for _, candidate := range row.Candidates {
			if err := validateCandidateDerivation(derivationKind, candidate); err != nil {
				return fmt.Errorf("callsite %s candidate %d: %w", c.CallsiteID, candidate.Ordinal, err)
			}
			candidateStepsJSON, _ := json.Marshal(candidateProvenance(steps, candidate))
			candidateMeta, _ := json.Marshal(map[string]any{
				"callsite_id": c.CallsiteID, "candidate_ordinal": candidate.Ordinal,
				"target_stable_id": candidate.TargetStableID, "target_native_id": candidate.TargetNativeID,
				"mechanism":      candidate.Mechanism,
				"declared_scope": candidate.DeclaredScope, "receiver_type": candidate.ReceiverType,
				"receiver_origin": candidate.ReceiverOrigin, "receiver_shape": candidate.ReceiverShape,
				"receiver_chain": candidate.ReceiverChain, "import_chain": candidate.ImportChain,
				"flow_source_stable_ids": candidate.FlowSourceStableIDs, "flow_edge_stable_ids": candidate.FlowEdgeStableIDs,
				"flow_source_facts": candidate.FlowSourceFacts, "flow_edge_facts": candidate.FlowEdgeFacts,
				"dynamic_dispatch": candidate.DynamicDispatch, "export_status": candidate.ExportStatus,
				"parser_complete": candidate.ParserComplete, "repository_revision": revision,
				"verification_status": candidate.VerificationStatus,
				"candidate_count":     c.CandidateCount, "sibling_count": c.CandidateCount,
				"derivation_kind": derivationKind, "evidence_set": evidenceSet,
				"derivation_contract":         ResolutionDerivationContract,
				"producer_build_id":           identity.BuildID,
				"producer_source_fingerprint": identity.SourceFingerprint,
			})
			// Candidate viability is a typed, set-valued derivation fact. NULL is
			// deliberate: zero would still persist an uncalibrated probability-like
			// scalar and invite generic confidence consumers to interpret policy as truth.
			if _, err := tx.Exec(`INSERT INTO edges
				(source_id,target_id,type,source_line,source_file,resolution_method,confidence,metadata,trust_tier,candidate_count,evidence_type,verification_status,
				 derivation_contract,derivation_kind,evidence_set,analysis_boundary,pass_kind,pass_version,pass_status,abstention_reason,sibling_count,
				 producer_build_id,producer_source_fingerprint,pass_coverage,provenance_steps,
				 declared_scope,receiver_type,receiver_origin,receiver_shape,receiver_chain,import_chain,export_status,parser_complete)
				VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
				callsiteNodeID, candidate.TargetID, "CANDIDATE", c.SourceLine, c.SourceFile,
				candidate.Mechanism, nil, string(candidateMeta),
				"CANDIDATE", c.CandidateCount, "resolver_candidate", candidate.VerificationStatus,
				ResolutionDerivationContract, derivationKind, evidenceSet, revision, passKind, "1", passStatus, "", c.CandidateCount,
				identity.BuildID, identity.SourceFingerprint, string(coverageJSON), string(candidateStepsJSON),
				candidate.DeclaredScope, candidate.ReceiverType, candidate.ReceiverOrigin, candidate.ReceiverShape,
				candidate.ReceiverChain, candidate.ImportChain, candidate.ExportStatus, candidate.ParserComplete); err != nil {
				return fmt.Errorf("insert candidate edge %s/%d: %w", c.CallsiteID, candidate.Ordinal, err)
			}
		}
	}
	return nil
}

// AttachResolutionGraph is the full-index wrapper for the transactional graph
// attachment API.
func (d *DB) AttachResolutionGraph(identity GraphCompletionIdentity, rows []AttachedResolution) error {
	tx, err := d.db.Begin()
	if err != nil {
		return fmt.Errorf("begin graph resolution tx: %w", err)
	}
	if err := AttachResolutionGraphTx(tx, identity, rows); err != nil {
		_ = tx.Rollback()
		return err
	}
	if err := BindGraphCompletionTx(tx, identity); err != nil {
		_ = tx.Rollback()
		return err
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit graph resolution tx: %w", err)
	}
	return nil
}

// QueryAttachedCandidates is the normal producer graph query for resolver
// evidence. It reads only primary nodes/edges, so graph consumers automatically
// observe retained candidates without joining the compatibility sidecar.
func (d *DB) QueryAttachedCandidates(callee string) ([]AttachedCandidate, error) {
	return d.QueryAttachedCandidatesWithPolicy(callee, ConservativeCandidatePolicy)
}

// QueryAttachedCandidatesWithPolicy applies an immutable query-time policy to
// typed graph facts. Policy output is never written back to the graph.
func (d *DB) QueryAttachedCandidatesWithPolicy(callee string, policy CandidateQueryPolicy) ([]AttachedCandidate, error) {
	return d.queryAttachedCandidates(callee, "", policy, "")
}

// ResolveCandidates is the canonical v2 query boundary. It rejects stale graph
// revisions and unknown policy versions before loading primary-graph facts.
func (d *DB) ResolveCandidates(callsiteID, policyVersionID, graphRevision string) ([]AttachedCandidate, error) {
	var policy CandidateQueryPolicy
	switch policyVersionID {
	case ConservativeCandidatePolicy.VersionID():
		policy = ConservativeCandidatePolicy
	case InspectAllCandidatePolicy.VersionID():
		policy = InspectAllCandidatePolicy
	default:
		return nil, fmt.Errorf("unknown query policy version %q", policyVersionID)
	}
	return d.queryAttachedCandidates("", callsiteID, policy, graphRevision)
}

func (d *DB) queryAttachedCandidates(callee, callsiteID string, policy CandidateQueryPolicy, requiredRevision string) ([]AttachedCandidate, error) {
	if err := policy.validate(); err != nil {
		return nil, err
	}
	var complete, revision string
	if err := d.db.QueryRow(`SELECT COALESCE(value,'') FROM project_meta WHERE key='graph_resolution_complete'`).Scan(&complete); err != nil {
		return nil, fmt.Errorf("graph-native resolution unavailable: %w", err)
	}
	if complete != "1" {
		return nil, fmt.Errorf("graph-native resolution is incomplete (state=%q)", complete)
	}
	if err := d.db.QueryRow(`SELECT COALESCE(value,'') FROM project_meta WHERE key='graph_resolution_revision'`).Scan(&revision); err != nil {
		return nil, fmt.Errorf("graph-native resolution revision unavailable: %w", err)
	}
	if revision == "" || revision == "stale" {
		return nil, fmt.Errorf("graph-native resolution has no current revision (revision=%q)", revision)
	}
	if requiredRevision != "" && requiredRevision != revision {
		return nil, fmt.Errorf("stale graph revision: requested %q current %q", requiredRevision, revision)
	}
	var receipt, receiptSHA string
	if err := d.db.QueryRow(`SELECT
		COALESCE((SELECT value FROM project_meta WHERE key='graph_completion_receipt'),''),
		COALESCE((SELECT value FROM project_meta WHERE key='graph_completion_receipt_sha256'),'')`).Scan(&receipt, &receiptSHA); err != nil {
		return nil, fmt.Errorf("graph completion receipt unavailable: %w", err)
	}
	sum := sha256.Sum256([]byte(receipt))
	if receipt == "" || receiptSHA != hex.EncodeToString(sum[:]) {
		return nil, fmt.Errorf("graph completion receipt hash mismatch")
	}
	var identity GraphCompletionIdentity
	if err := json.Unmarshal([]byte(receipt), &identity); err != nil {
		return nil, fmt.Errorf("decode graph completion receipt: %w", err)
	}
	if _, _, err := identity.receipt(); err != nil || identity.RepositoryRevision != revision {
		return nil, fmt.Errorf("graph completion identity does not bind revision %q", revision)
	}
	selector, selectorValue := "c.name=?", callee
	if callsiteID != "" {
		selector, selectorValue = "c.stable_id=?", callsiteID
	}
	query := `SELECT hc.source_id, c.id, COALESCE(ce.target_id,0), COALESCE(ce.target_symbol_id,''),
		c.qualified_name, c.file_path, c.start_line, c.name,
		COALESCE(c.candidate_state,''),
		COALESCE(ce.ordinal,-1),
		COALESCE(ce.resolution_method,''),
		COALESCE(hc.analysis_boundary,''),
		COALESCE(c.candidate_count_v2,0),
		CASE WHEN ce.id IS NULL THEN 0 ELSE 1 END,
		COALESCE(json_extract(hc.metadata,'$.structural_authority'),''),
		CASE WHEN c.candidate_state='selected' THEN 'source_supported' ELSE c.candidate_state END,
		COALESCE(ce.derivation_kind,hc.derivation_kind,''),
		COALESCE(ce.evidence_set,hc.evidence_set,''),
		COALESCE(ce.sibling_count,hc.sibling_count,0),
		COALESCE((SELECT u.reason_code FROM nodes u WHERE u.node_type='unresolved_fact' AND u.callsite_id=c.stable_id LIMIT 1),''),
		'[]', '[]',
		COALESCE(ce.declared_scope,''), COALESCE(ce.receiver_type,''), COALESCE(ce.receiver_origin,''),
		COALESCE(ce.receiver_shape,''), COALESCE(ce.receiver_chain,'[]'), COALESCE(ce.import_chain,'[]'),
		COALESCE(ce.export_status,''), ce.parser_complete
		FROM nodes c
		JOIN edges hc ON hc.target_id=c.id AND hc.type='HAS_CALLSITE'
		LEFT JOIN edges ce ON ce.source_id=c.id AND ce.type='CANDIDATE_TARGET'
		 AND ce.analysis_boundary=?
		WHERE c.label='Callsite' AND ` + selector + `
		  AND hc.analysis_boundary=?
		ORDER BY c.id, COALESCE(ce.ordinal,-1), ce.target_symbol_id`
	rows, err := d.db.Query(query, revision, selectorValue, revision)
	if err != nil {
		return nil, fmt.Errorf("query attached candidates: %w", err)
	}
	defer rows.Close()
	var out []AttachedCandidate
	for rows.Next() {
		var c AttachedCandidate
		var coverageJSON, stepsJSON string
		var parserComplete sql.NullBool
		if err := rows.Scan(&c.SourceID, &c.CallsiteNodeID, &c.TargetID, &c.TargetStableID, &c.CallsiteID,
			&c.SourceFile, &c.SourceLine, &c.Callee, &c.DispatchState,
			&c.CandidateOrdinal, &c.Mechanism, &c.Revision,
			&c.CandidateCount, &c.HasCandidate, &c.StructuralAuthority, &c.TargetAuthority,
			&c.DerivationKind, &c.EvidenceSet, &c.SiblingCount, &c.AbstentionReason,
			&coverageJSON, &stepsJSON, &c.DeclaredScope, &c.ReceiverType, &c.ReceiverOrigin,
			&c.ReceiverShape, &c.ReceiverChain, &c.ImportChain, &c.ExportStatus, &parserComplete); err != nil {
			return nil, err
		}
		if parserComplete.Valid {
			value := parserComplete.Bool
			c.ParserComplete = &value
		}
		if err := json.Unmarshal([]byte(coverageJSON), &c.PassCoverage); err != nil {
			return nil, fmt.Errorf("decode pass coverage for %s: %w", c.CallsiteID, err)
		}
		if err := json.Unmarshal([]byte(stepsJSON), &c.ProvenanceSteps); err != nil {
			return nil, fmt.Errorf("decode provenance for %s: %w", c.CallsiteID, err)
		}
		c.PolicyID, c.PolicyVersion, c.PolicyVersionID = policy.ID, policy.Version, policy.VersionID()
		c.PolicyHash = c.PolicyVersionID
		out = append(out, c)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if err := rows.Close(); err != nil {
		return nil, err
	}
	if err := d.loadResolutionV2Evidence(out); err != nil {
		return nil, err
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].CallsiteNodeID != out[j].CallsiteNodeID {
			return out[i].CallsiteNodeID < out[j].CallsiteNodeID
		}
		if out[i].CandidateOrdinal != out[j].CandidateOrdinal {
			return out[i].CandidateOrdinal < out[j].CandidateOrdinal
		}
		return out[i].TargetID < out[j].TargetID
	})
	rankByCallsite := make(map[int64]int)
	for i := range out {
		out[i].PolicyRank = rankByCallsite[out[i].CallsiteNodeID]
		rankByCallsite[out[i].CallsiteNodeID]++
		closedForDerivation := out[i].CompletenessStates[out[i].DerivationKind] == "closed"
		if policy == ConservativeCandidatePolicy && out[i].HasCandidate && out[i].CandidateCount == 1 && out[i].TargetAuthority == "source_supported" && closedForDerivation {
			out[i].PolicySelected = true
			out[i].PolicyReason = "unique_source_supported_candidate"
			out[i].MatchedRuleIDs = []string{policy.SelectionRuleID()}
		} else if policy == InspectAllCandidatePolicy {
			out[i].PolicyReason = "inspection_policy_never_selects"
			out[i].MatchedRuleIDs = []string{policy.SelectionRuleID()}
		} else {
			out[i].PolicyReason = "policy_abstained"
		}
	}
	return out, nil
}

// GetAllEdges returns every edge whose confidence is >= minConf, in stable id
// order. Used by the C7 closure pass to build its adjacency list. The closure
// package applies an additional verified-resolution-method override on top of
// this floor (RF-4), so passing a conservative minConf here (e.g. 0.0) is
// safe — the closure filter is the authoritative gate. resolution_method and
// confidence may be NULL on rows from very old binaries; COALESCE keeps the
// scan total.
func (d *DB) GetAllEdges(minConf float64) ([]*Edge, error) {
	rows, err := d.db.Query(
		`SELECT id, source_id, target_id, type, source_line, COALESCE(source_file, ''),
		        COALESCE(resolution_method, ''), COALESCE(confidence, 0.0)
		   FROM edges
		  WHERE COALESCE(confidence, 0.0) >= ?
		    AND type <> 'CANDIDATE'
		    AND NOT (
				 type IN ('HAS_CALLSITE', 'CANDIDATE')
				 AND COALESCE((SELECT value FROM project_meta WHERE key='graph_resolution_complete'), '0') <> '1'
			)`,
		minConf,
	)
	if err != nil {
		return nil, fmt.Errorf("query all edges: %w", err)
	}
	defer rows.Close()

	var edges []*Edge
	for rows.Next() {
		var e Edge
		if err := rows.Scan(&e.ID, &e.SourceID, &e.TargetID, &e.Type, &e.SourceLine,
			&e.SourceFile, &e.ResolutionMethod, &e.Confidence); err != nil {
			return nil, fmt.Errorf("scan edge: %w", err)
		}
		edges = append(edges, &e)
	}
	return edges, rows.Err()
}

// BatchInsertClosure inserts transitive-closure rows in a single transaction
// with a prepared statement, mirroring BatchInsertEdges. INSERT OR REPLACE so
// the (source_id, target_id, depth) primary key dedups deterministically if a
// row is somehow emitted twice.
func (d *DB) BatchInsertClosure(rows []*Closure) error {
	if len(rows) == 0 {
		return nil
	}
	tx, err := d.db.Begin()
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	stmt, err := tx.Prepare(
		`INSERT OR REPLACE INTO closure (source_id, target_id, depth, min_confidence)
		 VALUES (?, ?, ?, ?)`,
	)
	if err != nil {
		tx.Rollback()
		return fmt.Errorf("prepare: %w", err)
	}
	defer stmt.Close()

	for i, r := range rows {
		if _, err := stmt.Exec(r.SourceID, r.TargetID, r.Depth, r.MinConfidence); err != nil {
			tx.Rollback()
			return fmt.Errorf("insert closure %d: %w", i, err)
		}
	}
	return tx.Commit()
}

// ClosureCount returns total number of closure rows.
func (d *DB) ClosureCount() int {
	var count int
	d.db.QueryRow(`SELECT COUNT(*) FROM closure`).Scan(&count)
	return count
}

// ClearClosure deletes all rows from the closure table. Required before a
// closure REBUILD (the post-LSP -rebuild-closure pass): BatchInsertClosure
// uses INSERT OR REPLACE keyed on (source_id, target_id, depth), so without a
// clear it would leave behind stale pairs — reachability through edges the LSP
// resolve pass later DELETED or RE-POINTED (e.g. hono lost 1364 false pairs).
// A clean clear makes the recomputed closure exactly reflect the current edges.
func (d *DB) ClearClosure() error {
	_, err := d.db.Exec(`DELETE FROM closure`)
	return err
}

// LookupNodeByName finds nodes by name. Returns slice of node IDs.
func (d *DB) LookupNodeByName(name string) []int64 {
	rows, err := d.db.Query(`SELECT id FROM nodes WHERE name = ?`, name)
	if err != nil {
		return nil
	}
	defer rows.Close()
	var ids []int64
	for rows.Next() {
		var id int64
		rows.Scan(&id)
		ids = append(ids, id)
	}
	return ids
}

// UpdateParentID sets the parent_id for a node after batch insert.
func (d *DB) UpdateParentID(nodeID, parentID int64) {
	d.db.Exec("UPDATE nodes SET parent_id = ? WHERE id = ?", nullableParentID(parentID), nodeID)
}

// NodeCount returns total number of nodes.
func (d *DB) NodeCount() int {
	var count int
	d.db.QueryRow(`SELECT COUNT(*) FROM nodes`).Scan(&count)
	return count
}

// EdgeCount returns total number of edges.
func (d *DB) EdgeCount() int {
	var count int
	d.db.QueryRow(`SELECT COUNT(*) FROM edges`).Scan(&count)
	return count
}

// PropertyCount returns total number of properties.
func (d *DB) PropertyCount() int {
	var count int
	d.db.QueryRow(`SELECT COUNT(*) FROM properties`).Scan(&count)
	return count
}

// AssertionCount returns total number of assertions.
func (d *DB) AssertionCount() int {
	var count int
	d.db.QueryRow(`SELECT COUNT(*) FROM assertions`).Scan(&count)
	return count
}

// InsertProperty inserts a property for a node.
func (d *DB) InsertProperty(p *Property) error {
	_, err := d.db.Exec(
		`INSERT INTO properties (node_id, kind, value, line, confidence) VALUES (?, ?, ?, ?, ?)`,
		p.NodeID, p.Kind, p.Value, p.Line, p.Confidence,
	)
	return err
}

// InsertAssertion inserts an assertion from a test function.
func (d *DB) InsertAssertion(a *Assertion) error {
	// Column set MUST match BatchInsertAssertions (incl. resolution_score, the
	// multi-signal link score); omitting it silently defaulted every single-row
	// assertion to resolution_score = 0.0.
	_, err := d.db.Exec(
		`INSERT INTO assertions (test_node_id, target_node_id, resolution_score, kind, expression, expected, line) VALUES (?, ?, ?, ?, ?, ?, ?)`,
		a.TestNodeID, a.TargetNodeID, a.ResolutionScore, a.Kind, a.Expression, a.Expected, a.Line,
	)
	return err
}

// BatchInsertProperties inserts properties in a single transaction.
func (d *DB) BatchInsertProperties(props []*Property) error {
	if len(props) == 0 {
		return nil
	}
	tx, err := d.db.Begin()
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	stmt, err := tx.Prepare(
		`INSERT INTO properties (node_id, kind, value, line, confidence) VALUES (?, ?, ?, ?, ?)`,
	)
	if err != nil {
		tx.Rollback()
		return fmt.Errorf("prepare: %w", err)
	}
	defer stmt.Close()

	for i, p := range props {
		_, err := stmt.Exec(p.NodeID, p.Kind, p.Value, p.Line, p.Confidence)
		if err != nil {
			tx.Rollback()
			return fmt.Errorf("insert property %d: %w", i, err)
		}
	}
	return tx.Commit()
}

// BatchInsertAssertions inserts assertions in a single transaction.
func (d *DB) BatchInsertAssertions(assertions []*Assertion) error {
	if len(assertions) == 0 {
		return nil
	}
	tx, err := d.db.Begin()
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	stmt, err := tx.Prepare(
		`INSERT INTO assertions (test_node_id, target_node_id, resolution_score, kind, expression, expected, line) VALUES (?, ?, ?, ?, ?, ?, ?)`,
	)
	if err != nil {
		tx.Rollback()
		return fmt.Errorf("prepare: %w", err)
	}
	defer stmt.Close()

	for i, a := range assertions {
		_, err := stmt.Exec(a.TestNodeID, a.TargetNodeID, a.ResolutionScore, a.Kind, a.Expression, a.Expected, a.Line)
		if err != nil {
			tx.Rollback()
			return fmt.Errorf("insert assertion %d: %w", i, err)
		}
	}
	return tx.Commit()
}

// BatchInsertCochanges inserts co-change pairs in a single transaction.
// pairs maps [file_a, file_b] (canonical order) to co-occurrence count.
func (d *DB) BatchInsertCochanges(pairs map[[2]string]int) error {
	if len(pairs) == 0 {
		return nil
	}
	tx, err := d.db.Begin()
	if err != nil {
		return err
	}
	stmt, err := tx.Prepare("INSERT OR REPLACE INTO cochanges (file_a, file_b, count) VALUES (?, ?, ?)")
	if err != nil {
		tx.Rollback()
		return err
	}
	defer stmt.Close()
	for pair, count := range pairs {
		if _, err := stmt.Exec(pair[0], pair[1], count); err != nil {
			tx.Rollback()
			return err
		}
	}
	return tx.Commit()
}

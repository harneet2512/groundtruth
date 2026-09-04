package store

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
)

// Publication has two phases and two receipts.
//
// Phase 1 commits the core graph -- files, symbols and structural edges. Phase 2
// commits the resolution analysis: callsites, candidates, flow facts and
// completeness facts. A phase-2 failure rolls back as a unit and leaves phase 1
// on disk, so a graph can legitimately carry the core layer and no resolution
// evidence at all. That is not a corrupt graph and must not read as one: a
// repository whose analysis phase cannot finish within its budget still ships a
// core graph, and the receipt is what tells a reader which layer it got.
//
// Neither receipt is a field on GraphCompletionIdentity. Every field of that
// struct is required by its own verifier, so a core-only graph could not carry
// one, and adding an optional field would change a reader-visible contract.
// These are project_meta rows written beside it instead.
const (
	CorePhaseReceiptSchema     = "gt-index.core-phase.v1"
	AnalysisPhaseReceiptSchema = "gt-index.analysis-phase.v1"

	// project_meta keys, following the existing <subject>_<fact> convention of
	// graph_completion_receipt / graph_completion_receipt_sha256.
	CorePhaseStateKey             = "core_phase_state"
	CorePhaseReceiptKey           = "core_phase_receipt"
	CorePhaseReceiptSHA256Key     = "core_phase_receipt_sha256"
	AnalysisStateKey              = "analysis_state"
	AnalysisFailureReasonKey      = "analysis_failure_reason"
	AnalysisPhaseReceiptKey       = "analysis_phase_receipt"
	AnalysisPhaseReceiptSHA256Key = "analysis_phase_receipt_sha256"

	// CorePhaseCommitted is the only core state a published graph can carry: a
	// core failure publishes nothing, so an unpublished core state is unreadable
	// by construction.
	CorePhaseCommitted = "committed"

	// Analysis states. "not_run" and a complete analysis holding zero callsites
	// are deliberately different: the first says the pass never executed, the
	// second says it executed and found nothing. A reader that cannot tell those
	// apart cannot tell an empty repository from a skipped analysis.
	AnalysisStateComplete = "complete"
	AnalysisStateFailed   = "failed"
	AnalysisStateNotRun   = "not_run"

	// analysisStateUnrecorded is what a reader reports for a graph published
	// before this contract existed: absence of the row is not evidence that the
	// analysis ran.
	analysisStateUnrecorded = "unrecorded"
)

// ErrAnalysisPhaseAbsent is returned by every reader that verifies the graph
// completion receipt when the graph carries no resolution analysis. It is a
// named degraded state, not a storage error: the caller can fall back, and
// errors.Is lets it distinguish "this graph never had an analysis" from "this
// graph's receipt does not verify".
var ErrAnalysisPhaseAbsent = errors.New("graph has no resolution analysis phase")

// CorePhaseReceipt records the layer a phase-2 failure may never cost.
type CorePhaseReceipt struct {
	Schema              string `json:"schema"`
	State               string `json:"state"`
	RepositoryRevision  string `json:"repository_revision"`
	BuildID             string `json:"build_id"`
	SourceFingerprint   string `json:"source_fingerprint"`
	ExecutableSHA256    string `json:"executable_sha256"`
	FileCount           int    `json:"file_count"`
	SymbolCount         int    `json:"symbol_count"`
	StructuralEdgeCount int    `json:"structural_edge_count"`
}

// AnalysisPhaseReceipt records whether the analysis layer is present, and when
// it is not, the named reason and what was rolled back to get here.
type AnalysisPhaseReceipt struct {
	Schema             string   `json:"schema"`
	State              string   `json:"state"`
	RepositoryRevision string   `json:"repository_revision"`
	BuildID            string   `json:"build_id"`
	FailureReason      string   `json:"failure_reason"`
	RolledBack         []string `json:"rolled_back"`
	CallsiteCount      int      `json:"callsite_count"`
	CandidateCount     int      `json:"candidate_count"`
	ClosureRowCount    int      `json:"closure_row_count"`
}

// Seal returns the receipt payload and its SHA-256, mirroring how the graph
// completion receipt is bound. A receipt that does not describe a state a
// reader can act on is refused rather than published.
func (r CorePhaseReceipt) Seal() (string, string, error) {
	if r.Schema != CorePhaseReceiptSchema || r.State != CorePhaseCommitted ||
		r.RepositoryRevision == "" || r.BuildID == "" || r.SourceFingerprint == "" ||
		r.ExecutableSHA256 == "" {
		return "", "", fmt.Errorf("incomplete core phase receipt")
	}
	return sealReceipt(r)
}

// Seal returns the analysis receipt payload and its SHA-256. A failed analysis
// without a reason, or without a record of what it discarded, is not a receipt:
// it is the silence this item exists to remove.
func (r AnalysisPhaseReceipt) Seal() (string, string, error) {
	if r.Schema != AnalysisPhaseReceiptSchema || r.RepositoryRevision == "" || r.BuildID == "" {
		return "", "", fmt.Errorf("incomplete analysis phase receipt")
	}
	switch r.State {
	case AnalysisStateComplete:
		if r.FailureReason != "" || len(r.RolledBack) != 0 {
			return "", "", fmt.Errorf("complete analysis phase receipt carries a failure")
		}
	case AnalysisStateFailed:
		if r.FailureReason == "" || len(r.RolledBack) == 0 {
			return "", "", fmt.Errorf("failed analysis phase receipt names no reason or nothing rolled back")
		}
	case AnalysisStateNotRun:
		if r.CallsiteCount != 0 || r.CandidateCount != 0 {
			return "", "", fmt.Errorf("unrun analysis phase receipt carries counts")
		}
	default:
		return "", "", fmt.Errorf("unknown analysis phase state %q", r.State)
	}
	return sealReceipt(r)
}

func sealReceipt(v any) (string, string, error) {
	payload, err := json.Marshal(v)
	if err != nil {
		return "", "", err
	}
	sum := sha256.Sum256(payload)
	return string(payload), hex.EncodeToString(sum[:]), nil
}

// analysisAbsenceError turns a resolution row a core-only graph never wrote
// into the named degraded state. It returns nil when scanErr is a real storage
// error, so a genuine fault is never laundered into a degradation.
func analysisAbsenceError(db *sql.DB, scanErr error) error {
	if scanErr != nil && !errors.Is(scanErr, sql.ErrNoRows) {
		return nil
	}
	state := metaValueOrEmpty(db, AnalysisStateKey)
	if state == "" {
		state = analysisStateUnrecorded
	}
	if reason := metaValueOrEmpty(db, AnalysisFailureReasonKey); reason != "" {
		return fmt.Errorf("%w: analysis_state=%s analysis_failure_reason=%s (the core graph is readable; resolution evidence was not published)",
			ErrAnalysisPhaseAbsent, state, reason)
	}
	return fmt.Errorf("%w: analysis_state=%s (the core graph is readable; resolution evidence was not published)",
		ErrAnalysisPhaseAbsent, state)
}

func metaValueOrEmpty(db *sql.DB, key string) string {
	var value string
	if err := db.QueryRow(`SELECT COALESCE(value,'') FROM project_meta WHERE key=?`, key).Scan(&value); err != nil {
		return ""
	}
	return value
}

// AnalysisState reports the published analysis state and, when it failed, the
// recorded reason. A graph published before this contract existed reports
// "unrecorded": absence of the row is not evidence the analysis ran.
func (d *DB) AnalysisState() (state, reason string) {
	state = metaValueOrEmpty(d.db, AnalysisStateKey)
	if state == "" {
		state = analysisStateUnrecorded
	}
	return state, metaValueOrEmpty(d.db, AnalysisFailureReasonKey)
}

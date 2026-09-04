package store

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"path/filepath"
	"strings"
	"testing"
)

func completeCoreReceipt() CorePhaseReceipt {
	return CorePhaseReceipt{
		Schema: CorePhaseReceiptSchema, State: CorePhaseCommitted,
		RepositoryRevision: "rev", BuildID: "build", SourceFingerprint: "source",
		ExecutableSHA256: "exe", FileCount: 458, SymbolCount: 3511, StructuralEdgeCount: 9149,
	}
}

func TestCorePhaseReceiptBindsItsOwnPayload(t *testing.T) {
	payload, sum, err := completeCoreReceipt().Seal()
	if err != nil {
		t.Fatal(err)
	}
	want := sha256.Sum256([]byte(payload))
	if sum != hex.EncodeToString(want[:]) {
		t.Fatal("core phase receipt hash does not bind its payload")
	}
	if !strings.Contains(payload, `"structural_edge_count":9149`) {
		t.Fatalf("core receipt does not carry the layer it attests: %s", payload)
	}
}

func TestIncompleteCorePhaseReceiptIsRefused(t *testing.T) {
	incomplete := completeCoreReceipt()
	incomplete.BuildID = ""
	if _, _, err := incomplete.Seal(); err == nil {
		t.Fatal("a core phase receipt with no producer identity was sealed")
	}
}

// A receipt that claims a state it does not describe is worse than no receipt:
// it is a degradation a reader cannot act on.
func TestAnalysisPhaseReceiptRefusesStatesItDoesNotDescribe(t *testing.T) {
	base := AnalysisPhaseReceipt{
		Schema: AnalysisPhaseReceiptSchema, RepositoryRevision: "rev", BuildID: "build",
	}
	failedNoReason := base
	failedNoReason.State = AnalysisStateFailed
	if _, _, err := failedNoReason.Seal(); err == nil {
		t.Fatal("a failed analysis receipt naming no reason was sealed")
	}
	failedNoRollback := base
	failedNoRollback.State = AnalysisStateFailed
	failedNoRollback.FailureReason = "attach graph-native resolution evidence"
	if _, _, err := failedNoRollback.Seal(); err == nil {
		t.Fatal("a failed analysis receipt recording nothing rolled back was sealed")
	}
	completeWithFailure := base
	completeWithFailure.State = AnalysisStateComplete
	completeWithFailure.FailureReason = "something"
	if _, _, err := completeWithFailure.Seal(); err == nil {
		t.Fatal("a complete analysis receipt carrying a failure was sealed")
	}
	unknown := base
	unknown.State = "degraded"
	if _, _, err := unknown.Seal(); err == nil {
		t.Fatal("an analysis receipt with an unknown state was sealed")
	}
}

// "ran and found nothing" and "never ran" are different facts, and the receipt
// is what keeps them apart: both have zero callsites, only one has a state that
// says the pass executed.
func TestEmptyAnalysisIsNotTheSameStateAsAnUnrunOne(t *testing.T) {
	empty := AnalysisPhaseReceipt{
		Schema: AnalysisPhaseReceiptSchema, State: AnalysisStateComplete,
		RepositoryRevision: "rev", BuildID: "build",
	}
	emptyPayload, _, err := empty.Seal()
	if err != nil {
		t.Fatal(err)
	}
	unrun := AnalysisPhaseReceipt{
		Schema: AnalysisPhaseReceiptSchema, State: AnalysisStateNotRun,
		RepositoryRevision: "rev", BuildID: "build", FailureReason: "operator_skipped_analysis",
	}
	unrunPayload, _, err := unrun.Seal()
	if err != nil {
		t.Fatal(err)
	}
	if emptyPayload == unrunPayload {
		t.Fatal("an empty analysis and an unrun analysis seal to the same receipt")
	}
	if !strings.Contains(emptyPayload, `"state":"complete"`) || !strings.Contains(emptyPayload, `"callsite_count":0`) {
		t.Fatalf("an analysis that found nothing does not say so: %s", emptyPayload)
	}
	if !strings.Contains(unrunPayload, `"state":"not_run"`) {
		t.Fatalf("an unrun analysis does not say so: %s", unrunPayload)
	}
}

// A Go reader must be able to branch on which degraded state it got, not only
// learn that the analysis is absent.
func TestAnalysisStateIsReadableAndAbsenceIsTyped(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "state.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if state, reason := db.AnalysisState(); state != "unrecorded" || reason != "" {
		t.Fatalf("a graph with no analysis row reported state=%q reason=%q", state, reason)
	}
	if err := db.SetMeta(AnalysisStateKey, AnalysisStateFailed); err != nil {
		t.Fatal(err)
	}
	if err := db.SetMeta(AnalysisFailureReasonKey, "attach_graph_native_resolution_evidence"); err != nil {
		t.Fatal(err)
	}
	state, reason := db.AnalysisState()
	if state != AnalysisStateFailed || reason != "attach_graph_native_resolution_evidence" {
		t.Fatalf("analysis state round-trip returned state=%q reason=%q", state, reason)
	}
	if _, err := db.QueryAttachedCandidates("anything"); !errors.Is(err, ErrAnalysisPhaseAbsent) {
		t.Fatalf("a core-only graph did not return a typed absence: %v", err)
	}
}

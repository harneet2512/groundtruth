package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/harneet2512/groundtruth/gt-index/internal/resolver"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

// Publication is two phases with separate receipts.
//
// Phase 1 commits the core graph: the files and symbols already inserted, plus
// the structural CALLS edges. Phase 2 commits the resolution analysis. They used
// to be one transaction, so one candidate the store refused to derive discarded
// the whole index -- and a repository whose analysis cannot finish inside its
// budget shipped nothing at all rather than a readable core graph.
//
// The split is a wrapper around the existing phase bodies, not a rewrite of
// them: publishAnalysisPhase carries the publication loop verbatim so that the
// concurrent work on VTA budgets, flow-fact persistence and prepared-statement
// reuse inside that loop still applies.

// analysisPhaseAbort is how an abort raised inside the phase-2 body reaches the
// single recovery point. The body calls abortStagedBuild in a dozen places and
// each one first rolls the analysis transaction back; scoping the abort rather
// than rewriting those call sites is what keeps the body identical to the one
// this function was lifted from.
type analysisPhaseAbort struct{ reason string }

func (a analysisPhaseAbort) Error() string { return a.reason }

// analysisPhaseActive makes abortStagedBuild analysis-scoped for the duration
// of phase 2. Publication is single-goroutine by this point -- the parse workers
// have joined -- so a package-level flag is the whole synchronisation story.
var analysisPhaseActive bool

type analysisPhaseInput struct {
	db                 *store.DB
	stagedOutput       string
	allNodePtrs        []*store.Node
	allNodes           []store.Node
	nodeDBIDs          []int64
	callsites          []resolver.ResolutionCallsite
	hierarchyByOrdinal map[int]resolver.CHAThenRTAResult
	vtaByOrdinal       map[int]resolver.VTAResult
	mergeIDs           func(left, right []int64) []int64
	repositoryRevision string
	repositoryID       string
	producerIdentity   buildIdentity
	coreElapsed        time.Duration
}

// analysisPhaseResult is what the analysis receipt is built from. A failure
// carries the reason and what the rollback discarded, because "analysis absent"
// without either is the silence this item exists to remove.
type analysisPhaseResult struct {
	State string
	// Reason is why the analysis layer is absent: the abort text for a failed
	// phase, or the named skip for one that was never run. It is empty for a
	// complete analysis, where absence is not a fact that needs explaining.
	Reason         string
	RolledBack     []string
	CallsiteCount  int
	CandidateCount int
}

// analysisPhaseSkipReason reports why phase 2 must not start, or "" to run it.
// gnx degrades by skipping graph phases; the receipt is what turns that skip
// into a state a reader can act on instead of a silence. GT_SKIP_ANALYSIS is
// the operator's explicit form -- the escape hatch for a repository whose
// analysis cannot finish inside the run budget, which today publishes nothing
// at all rather than a core graph. GT_ANALYSIS_DEADLINE_SECONDS is the same
// decision taken from the clock: if extraction and resolution have already
// consumed the run, phase 2 is not started rather than started and abandoned.
func analysisPhaseSkipReason(coreElapsed time.Duration) (string, error) {
	if os.Getenv("GT_SKIP_ANALYSIS") == "1" {
		return "operator_skipped_analysis", nil
	}
	raw := os.Getenv("GT_ANALYSIS_DEADLINE_SECONDS")
	if raw == "" {
		return "", nil
	}
	seconds, err := strconv.Atoi(raw)
	if err != nil || seconds <= 0 {
		return "", fmt.Errorf("GT_ANALYSIS_DEADLINE_SECONDS=%q is not a positive number of seconds", raw)
	}
	if coreElapsed >= time.Duration(seconds)*time.Second {
		return fmt.Sprintf("core_phase_consumed_analysis_deadline_seconds=%d", seconds), nil
	}
	return "", nil
}

// analysisRollbackScope names every table group the phase-2 transaction owns.
// It is what a failed receipt reports as discarded, and it is the checklist for
// "rolled back as a unit": nothing in this list may survive a phase-2 failure.
var analysisRollbackScope = []string{
	"resolution_symbols", "resolution_callsites", "resolution_candidates",
	"Callsite nodes", "HAS_CALLSITE edges", "CANDIDATE_TARGET edges",
	"DerivationFact nodes", "CompletenessFact nodes", "flow fact nodes",
	"graph_completion_receipt", "resolution_complete",
}

// publishCorePhase commits the core graph on its own. A core failure still
// publishes nothing, exactly as today: there is no degraded state below this
// layer, so abortStagedBuild discards the staged build.
func publishCorePhase(db *store.DB, stagedOutput string, structural []*store.Edge) {
	coreTx, err := db.BeginTx()
	if err != nil {
		abortStagedBuild(db, stagedOutput, "begin core publication transaction: %v", err)
	}
	if err := store.BatchInsertEdgesTx(coreTx, structural); err != nil {
		_ = coreTx.Rollback()
		abortStagedBuild(db, stagedOutput, "batch insert edges: %v", err)
	}
	if err := coreTx.Commit(); err != nil {
		abortStagedBuild(db, stagedOutput, "commit core publication transaction: %v", err)
	}
}

// publishAnalysisPhase commits the resolution graph in its own transaction. A
// failure inside it rolls back as a unit and returns a named result; the core
// graph committed by phase 1 is already durable and is never touched here.
func publishAnalysisPhase(in analysisPhaseInput) (result analysisPhaseResult) {
	db, stagedOutput := in.db, in.stagedOutput
	allNodePtrs, allNodes, nodeDBIDs := in.allNodePtrs, in.allNodes, in.nodeDBIDs
	callsites := in.callsites
	hierarchyByOrdinal, vtaByOrdinal := in.hierarchyByOrdinal, in.vtaByOrdinal
	mergeIDs := in.mergeIDs
	repositoryRevision, repositoryID := in.repositoryRevision, in.repositoryID
	producerIdentity := in.producerIdentity

	// A repository whose analysis cannot finish inside the run budget publishes
	// nothing today. With the phases split it can publish the core graph and say
	// the analysis was never attempted -- which is a different fact from an
	// analysis that ran and found nothing, and the receipt keeps them apart.
	skip, skipErr := analysisPhaseSkipReason(in.coreElapsed)
	if skipErr != nil {
		// A malformed operator setting fails closed, and it fails as a core
		// failure: this runs before any analysis state has been decided.
		abortStagedBuild(db, stagedOutput, "%v", skipErr)
	}
	if skip != "" {
		fmt.Fprintf(os.Stderr, "  [analysis phase] not run: %s\n", skip)
		return analysisPhaseResult{State: store.AnalysisStateNotRun, Reason: skip}
	}

	analysisPhaseActive = true
	defer func() {
		analysisPhaseActive = false
		recovered := recover()
		if recovered == nil {
			return
		}
		abort, ok := recovered.(analysisPhaseAbort)
		if !ok {
			panic(recovered)
		}
		fmt.Fprintf(os.Stderr, "  [analysis phase] FAILED, rolled back: %s\n", abort.reason)
		result = analysisPhaseResult{
			State:      store.AnalysisStateFailed,
			Reason:     abort.reason,
			RolledBack: analysisRollbackScope,
		}
	}()

	publishTx, err := db.BeginTx()
	if err != nil {
		abortStagedBuild(db, stagedOutput, "begin analysis publication transaction: %v", err)
	}
	resolutionSymbols := make([]*store.ResolutionSymbol, 0, len(allNodePtrs))
	symbolByID := make(map[int64]store.ResolutionSymbol, len(allNodePtrs))
	for i, node := range allNodePtrs {
		if i >= len(nodeDBIDs) {
			continue
		}
		s := store.BuildResolutionSymbol(strconv.FormatInt(nodeDBIDs[i], 10), *node)
		resolutionSymbols = append(resolutionSymbols, &s)
		symbolByID[nodeDBIDs[i]] = s
	}
	if err := store.BatchInsertResolutionSymbolsTx(publishTx, resolutionSymbols); err != nil {
		_ = publishTx.Rollback()
		abortStagedBuild(db, stagedOutput, "insert resolution symbols: %v", err)
	}
	callsiteRows := make([]*store.ResolutionCallsite, 0, len(callsites))
	candidateRows := make([]*store.ResolutionCandidate, 0)
	graphRows := make([]store.AttachedResolution, 0, len(callsites))
	nodeByID := make(map[int64]*store.Node, len(allNodes))
	fileNodeIDByPath := make(map[string]int64)
	fileIdentityByPath := make(map[string]string)
	for i := range allNodes {
		if i < len(nodeDBIDs) {
			n := allNodes[i]
			n.ID = nodeDBIDs[i]
			nodeByID[nodeDBIDs[i]] = &n
			if n.Label == "Module" && n.FilePath != "" {
				fileNodeIDByPath[n.FilePath] = n.ID
				if symbol, ok := symbolByID[n.ID]; ok {
					fileIdentityByPath[n.FilePath] = symbol.StableID
				}
			}
		}
	}
	for _, c := range callsites {
		source, ok := symbolByID[c.SourceNodeID]
		if !ok {
			_ = publishTx.Rollback()
			abortStagedBuild(db, stagedOutput, "callsite ordinal %d source node %d missing from graph", c.CallsiteOrdinal, c.SourceNodeID)
		}
		callsiteID := store.StableResolutionCallsiteIDWithOrdinal(repositoryRevision, source.StableID, c.SourceFile, c.SourceLine, c.SourceLine, c.CallsiteOrdinal, c.Callee)
		candidateNodeIDs := append([]int64(nil), c.CandidateNodeIDs...)
		selectedNodeID := c.SelectedTargetNodeID
		publishedDispatchState := string(c.DispatchState)
		publishedMechanism := c.Mechanism
		vtaUsed := false
		if vta, ok := vtaByOrdinal[c.CallsiteOrdinal]; ok && vta.Completeness != "not_run" && (c.DispatchForm == "interface" || c.DispatchForm == "virtual") {
			if vta.Completeness == "partial" {
				// Incomplete flow must not erase conservative hierarchy evidence.
				if hierarchy, hierarchyOK := hierarchyByOrdinal[c.CallsiteOrdinal]; hierarchyOK {
					candidateNodeIDs = mergeIDs(vta.CandidateNodeIDs, hierarchy.CHACandidateNodeIDs)
				} else {
					candidateNodeIDs = append([]int64(nil), vta.CandidateNodeIDs...)
				}
			} else {
				// A closed flow fact is the narrower source-bound candidate set.
				candidateNodeIDs = append([]int64(nil), vta.CandidateNodeIDs...)
			}
			selectedNodeID = nil
			if mechanism := vtaCallsiteMechanism(vta.CandidateNodeIDs, candidateNodeIDs); mechanism != "" {
				publishedMechanism = mechanism
				vtaUsed = true
			}
			publishedDispatchState = candidateDispatchStateForCount(len(candidateNodeIDs))
		}
		if !vtaUsed {
			if hierarchy, ok := hierarchyByOrdinal[c.CallsiteOrdinal]; ok && (c.DispatchForm == "interface" || c.DispatchForm == "virtual") && (len(hierarchy.CHACandidateNodeIDs) > 0 || (hierarchy.ReceiverScoped && hierarchy.CHACompleteness == "closed")) {
				candidateNodeIDs = append([]int64(nil), hierarchy.CHACandidateNodeIDs...)
				selectedNodeID = nil
				if len(candidateNodeIDs) == 0 {
					publishedDispatchState = string(resolver.DispatchZero)
				} else if len(candidateNodeIDs) > 1 {
					publishedDispatchState = string(resolver.DispatchAmbiguous)
				} else {
					publishedDispatchState = string(resolver.DispatchCandidateOnly)
				}
			}
		}
		// Collapse candidates that denote the same symbol identity before anything
		// downstream counts them: the published candidate edge ids are keyed on the
		// target stable id, and candidate_count feeds the dispatch state.
		candidateNodeIDs = dedupeCandidatesByStableIdentity(candidateNodeIDs, selectedNodeID, func(id int64) (string, bool) {
			symbol, ok := symbolByID[id]
			if !ok {
				return "", false
			}
			return symbol.StableID, true
		})
		// Only restate a dispatch state that was itself derived from the candidate
		// count. dynamic, parser_incomplete and external_unresolved say something
		// the count cannot, and must survive the collapse untouched.
		switch publishedDispatchState {
		case string(resolver.DispatchZero), string(resolver.DispatchCandidateOnly), string(resolver.DispatchAmbiguous):
			publishedDispatchState = candidateDispatchStateForCount(len(candidateNodeIDs))
		}

		// Only a unique dispatch may carry selected-target authority. Some
		// conservative resolver paths retain a best-effort target while correctly
		// classifying the callsite as candidate_only. Publishing that internal hint
		// as Selected contradicts the resolution-v2 contract and used to abort the
		// entire atomic graph publication. Drop the hint at the producer boundary;
		// all candidates remain conserved and visible as unselected evidence.
		selectedNodeID = selectedTargetForDispatch(publishedDispatchState, selectedNodeID)
		var selectedStable, selectedNative *string
		if selectedNodeID != nil {
			if target, exists := symbolByID[*selectedNodeID]; exists {
				stable, native := target.StableID, target.NativeID
				selectedStable, selectedNative = &stable, &native
			}
		}
		passCoverage := make([]store.ResolutionPassCoverage, 0, len(c.PassExecutions))
		for _, pass := range c.PassExecutions {
			passCoverage = append(passCoverage, store.ResolutionPassCoverage{PassKind: pass.PassKind, Version: "1", Status: pass.Status, Reason: pass.Reason})
		}
		if vta, ok := vtaByOrdinal[c.CallsiteOrdinal]; ok && vta.Completeness != "not_run" {
			vtaStatus := "completed_match"
			if vta.Completeness == "partial" {
				vtaStatus = "partial"
			}
			if len(vta.CandidateNodeIDs) == 0 {
				vtaStatus = "completed_no_match"
				if vta.Completeness == "partial" {
					vtaStatus = "partial_no_match"
				}
			}
			vtaStableIDs := make([]string, 0, len(vta.CandidateNodeIDs))
			for _, id := range vta.CandidateNodeIDs {
				if symbol, exists := symbolByID[id]; exists {
					vtaStableIDs = append(vtaStableIDs, symbol.StableID)
				}
			}
			flowStableIDs := make([]string, 0, len(vta.FlowTypeNodeIDs))
			for _, id := range vta.FlowTypeNodeIDs {
				if symbol, exists := symbolByID[id]; exists {
					flowStableIDs = append(flowStableIDs, symbol.StableID)
				}
			}
			flowReason := vta.AbstentionReason
			if len(flowStableIDs) > 0 {
				flowReason = "flow_type_stable_ids=" + strings.Join(flowStableIDs, ",") + "; " + flowReason
			}
			passCoverage = append(passCoverage, store.ResolutionPassCoverage{
				PassKind: "vta", Version: "1", Status: vtaStatus,
				Reason: flowReason, CandidateStableIDs: vtaStableIDs, FlowTypeStableIDs: flowStableIDs,
			})
		}
		if hierarchy, ok := hierarchyByOrdinal[c.CallsiteOrdinal]; ok && (c.DispatchForm == "interface" || c.DispatchForm == "virtual") {
			chaStatus := "partial"
			chaReason := "hierarchy_open"
			if hierarchy.CHACompleteness == "closed" {
				chaReason = ""
				if len(hierarchy.CHACandidateNodeIDs) == 0 {
					chaStatus = "completed_no_match"
				} else {
					chaStatus = "completed_match"
				}
			}
			rtaStatus := "partial"
			rtaReason := hierarchy.RTAAbstentionReason
			if hierarchy.RTACompleteness == "closed" {
				rtaReason = ""
				if len(hierarchy.RTACandidateNodeIDs) == 0 {
					rtaStatus = "completed_no_match"
				} else {
					rtaStatus = "completed_match"
				}
			}
			stableIDs := func(ids []int64) []string {
				out := make([]string, 0, len(ids))
				for _, id := range ids {
					if symbol, exists := symbolByID[id]; exists {
						out = append(out, symbol.StableID)
					}
				}
				return out
			}
			passCoverage = append(passCoverage,
				store.ResolutionPassCoverage{PassKind: "cha", Version: "1", Status: chaStatus, Reason: chaReason, CandidateStableIDs: stableIDs(hierarchy.CHACandidateNodeIDs)},
				store.ResolutionPassCoverage{PassKind: "rta", Version: "1", Status: rtaStatus, Reason: rtaReason, CandidateStableIDs: stableIDs(hierarchy.RTACandidateNodeIDs), AllocationTypeStableIDs: stableIDs(hierarchy.RTAAllocationTypeIDs), ReachableStableIDs: stableIDs(hierarchy.ReachableNodeIDs), RootStableIDs: stableIDs(hierarchy.RootNodeIDs), RootPolicy: hierarchy.RootPolicy, RootPolicyVersion: hierarchy.RootPolicyVersion, RootCompleteness: hierarchy.RootCompleteness, RootBoundary: hierarchy.RootBoundary},
			)
		}
		callsiteRows = append(callsiteRows, &store.ResolutionCallsite{CallsiteID: callsiteID, CallsiteOrdinal: c.CallsiteOrdinal, RepositoryRevision: repositoryRevision, SourceStableID: source.StableID, SourceNativeID: source.NativeID, SourceID: c.SourceNodeID, SourceLine: c.SourceLine, SourceFile: c.SourceFile, Callee: c.Callee, Language: source.Language, DispatchState: publishedDispatchState, CandidateCount: len(candidateNodeIDs), SelectedTargetStableID: selectedStable, SelectedTargetNativeID: selectedNative, Mechanism: publishedMechanism, VerificationStatus: c.VerificationStatus, PassCoverage: passCoverage})
		var vtaProofs map[int64]resolver.VTAFlowProof
		if vta, ok := vtaByOrdinal[c.CallsiteOrdinal]; ok && vta.Completeness != "not_run" {
			var err error
			vtaProofs, err = candidateVTAProofs(vta)
			if err != nil {
				_ = publishTx.Rollback()
				abortStagedBuild(db, stagedOutput, "callsite %s VTA proof conservation failed: %v", callsiteID, err)
			}
		}
		graphCandidates := make([]*store.ResolutionCandidate, 0, len(candidateNodeIDs))
		for ordinal, targetID := range candidateNodeIDs {
			target, exists := symbolByID[targetID]
			if !exists {
				continue
			}
			complete := c.ParserComplete
			receiverChain, _ := json.Marshal(qualifiedCallChain(c.CalleeQualified))
			importChain, _ := json.Marshal(c.CandidateImportChains[targetID])
			receiverType, receiverOrigin := c.ReceiverType, c.ReceiverOrigin
			if vta, ok := vtaByOrdinal[c.CallsiteOrdinal]; ok && vta.Completeness != "not_run" && len(vta.FlowTypeNodeIDs) > 0 {
				flowStableIDs := make([]string, 0, len(vta.FlowTypeNodeIDs))
				flowNames := make([]string, 0, len(vta.FlowTypeNodeIDs))
				for _, flowID := range vta.FlowTypeNodeIDs {
					if symbol, exists := symbolByID[flowID]; exists {
						flowStableIDs = append(flowStableIDs, symbol.StableID)
						flowNames = append(flowNames, symbol.QualifiedName)
					}
				}
				receiverType = strings.Join(flowNames, ",")
				receiverOrigin = "vta_flow_stable_ids=" + strings.Join(flowStableIDs, ",")
			}
			var flowSourceStableIDs, flowEdgeStableIDs []string
			var flowSourceFacts, flowEdgeFacts []store.ResolutionFlowFact
			if _, ok := vtaByOrdinal[c.CallsiteOrdinal]; ok {
				proof := vtaProofs[targetID]
				for _, sourceID := range proof.SourceStableIDs {
					stableID := candidateVTAFactID("vta_source_", sourceID, callsiteID, target.StableID)
					flowSourceStableIDs = append(flowSourceStableIDs, stableID)
					flowSourceFacts = append(flowSourceFacts, store.ResolutionFlowFact{StableID: stableID, Kind: "source", CallsiteID: callsiteID, TargetStableID: target.StableID, Payload: "source=" + sourceID})
				}
				for _, edgeID := range proof.EdgeStableIDs {
					stableID := candidateVTAFactID("vta_edge_", edgeID, callsiteID, target.StableID)
					flowEdgeStableIDs = append(flowEdgeStableIDs, stableID)
					flowEdgeFacts = append(flowEdgeFacts, store.ResolutionFlowFact{StableID: stableID, Kind: "edge", CallsiteID: callsiteID, TargetStableID: target.StableID, Payload: "edge=" + edgeID})
				}
			}
			candidate := &store.ResolutionCandidate{CallsiteID: callsiteID, TargetID: targetID, TargetStableID: target.StableID, TargetNativeID: target.NativeID, Ordinal: ordinal, Mechanism: publishedMechanism, DeclaredScope: target.QualifiedName, ReceiverType: receiverType, ReceiverOrigin: receiverOrigin, ReceiverShape: c.CalleeQualified, ReceiverChain: string(receiverChain), ImportChain: string(importChain), FlowSourceStableIDs: flowSourceStableIDs, FlowEdgeStableIDs: flowEdgeStableIDs, FlowSourceFacts: flowSourceFacts, FlowEdgeFacts: flowEdgeFacts, DynamicDispatch: publishedDispatchState == string(resolver.DispatchDynamic), ExportStatus: target.ExportStatus, ParserComplete: &complete, VerificationStatus: c.VerificationStatus, Selected: selectedNodeID != nil && *selectedNodeID == targetID}
			candidateRows = append(candidateRows, candidate)
			graphCandidates = append(graphCandidates, candidate)
		}
		if len(graphCandidates) != len(candidateNodeIDs) {
			_ = publishTx.Rollback()
			abortStagedBuild(db, stagedOutput, "callsite %s candidate conservation failed: retained=%d expected=%d", callsiteID, len(graphCandidates), len(candidateNodeIDs))
		}
		if sourceNode := nodeByID[c.SourceNodeID]; sourceNode != nil {
			parseState := "complete"
			if !c.ParserComplete {
				parseState = "recovered"
			}
			graphCallsite := &store.ResolutionCallsite{
				CallsiteID: callsiteID, CallsiteOrdinal: c.CallsiteOrdinal, RepositoryRevision: repositoryRevision,
				SourceStableID: source.StableID, SourceNativeID: source.NativeID, SourceID: c.SourceNodeID,
				SourceLine: c.SourceLine, SourceFile: c.SourceFile, Callee: c.Callee, Language: source.Language,
				DispatchState: publishedDispatchState, CandidateCount: len(graphCandidates),
				SelectedTargetStableID: selectedStable, SelectedTargetNativeID: selectedNative,
				Mechanism: publishedMechanism, VerificationStatus: c.VerificationStatus,
				RepoID: repositoryID, FileNodeID: fileNodeIDByPath[c.SourceFile], FileIdentity: fileIdentityByPath[c.SourceFile], CallerSymbolID: source.StableID,
				ASTPath: c.ASTPath, ByteStart: c.ByteStart, ByteEnd: c.ByteEnd, ColumnStart: c.ColumnStart,
				DispatchForm: c.DispatchForm, ArgumentArity: c.ArgumentArity, ParseState: parseState,
				PassCoverage: passCoverage,
			}
			graphRows = append(graphRows, store.AttachedResolution{Callsite: graphCallsite, Source: sourceNode, Candidates: graphCandidates})
		}
	}
	if err := store.BatchInsertResolutionCallsitesTx(publishTx, callsiteRows); err != nil {
		_ = publishTx.Rollback()
		abortStagedBuild(db, stagedOutput, "insert resolution callsites: %v", err)
	}
	if err := store.BatchInsertResolutionCandidatesTx(publishTx, candidateRows); err != nil {
		_ = publishTx.Rollback()
		abortStagedBuild(db, stagedOutput, "insert resolution candidates: %v", err)
	}
	graphIdentity := store.GraphCompletionIdentity{
		Schema: store.GraphCompletionSchema, RepositoryRevision: repositoryRevision,
		BuildInfoSchema: producerIdentity.Schema, BuildID: producerIdentity.BuildID,
		GitCommit: producerIdentity.GitCommit, BuildTimeUTC: producerIdentity.BuildTimeUTC,
		SourceFingerprint: producerIdentity.SourceFingerprint, ExecutableSHA256: producerIdentity.ExecutableSHA256,
		GoToolchain: producerIdentity.GoToolchain, BuildTags: producerIdentity.BuildTags,
		GraphSchemaVersion: producerIdentity.SchemaVersion, ResolutionContract: store.CallResolutionContractV2,
		ResolutionAuthoritySchema: resolver.ResolutionAuthoritySchema,
		Complete:                  producerIdentity.Complete,
	}
	// Fault injection for the two-phase publication contract, off unless the
	// operator sets it. The injected fault is the real one this item exists for
	// -- a candidate the derivation mapper refuses -- raised inside the analysis
	// transaction so the rollback, the receipt and the exit code are all
	// exercised end to end rather than asserted about.
	if os.Getenv("GT_INJECT_ANALYSIS_FAILURE") == "1" {
		injectAnalysisFault(db, stagedOutput, graphRows)
	}
	if err := store.AttachResolutionGraphTx(publishTx, graphIdentity, graphRows); err != nil {
		_ = publishTx.Rollback()
		abortStagedBuild(db, stagedOutput, "attach graph-native resolution evidence: %v", err)
	}
	if err := store.BindGraphCompletionTx(publishTx, graphIdentity); err != nil {
		_ = publishTx.Rollback()
		abortStagedBuild(db, stagedOutput, "bind graph completion to producer identity: %v", err)
	}
	metadata := map[string]string{
		"resolution_schema_version":      "2",
		"resolution_producer_contract":   store.CallResolutionContractV2,
		"resolution_repository_revision": repositoryRevision,
		"resolution_complete":            "1",
	}
	for key, value := range metadata {
		if _, err := publishTx.Exec(`INSERT INTO project_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value`, key, value); err != nil {
			_ = publishTx.Rollback()
			abortStagedBuild(db, stagedOutput, "set resolution metadata %s: %v", key, err)
		}
	}
	if err := publishTx.Commit(); err != nil {
		abortStagedBuild(db, stagedOutput, "commit analysis publication transaction: %v", err)
	}
	return analysisPhaseResult{
		State:          store.AnalysisStateComplete,
		CallsiteCount:  len(callsiteRows),
		CandidateCount: len(candidateRows),
	}
}

// injectAnalysisFault poisons one already-built candidate so that
// AttachResolutionGraphTx rejects it exactly as it rejects a real one: by
// removing the native identity validateCandidateDerivation requires. It never
// runs unless GT_INJECT_ANALYSIS_FAILURE=1.
func injectAnalysisFault(db *store.DB, stagedOutput string, rows []store.AttachedResolution) {
	for _, row := range rows {
		if len(row.Candidates) == 0 || row.Candidates[0] == nil {
			continue
		}
		row.Candidates[0].TargetNativeID = ""
		return
	}
	abortStagedBuild(db, stagedOutput, "GT_INJECT_ANALYSIS_FAILURE=1 but no candidate was published to poison")
}

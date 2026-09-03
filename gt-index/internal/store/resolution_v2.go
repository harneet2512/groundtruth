package store

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
)

type resolutionV2Candidate struct {
	Candidate *ResolutionCandidate
	EdgeID    string
	FactID    string
	PassKind  string
}

type resolutionV2Fact struct {
	ID                      string
	PassKind                string
	Status                  string
	BoundaryKind            string
	BoundaryID              string
	Covered                 int
	Known                   *int
	Reason                  string
	CandidateStableIDs      []string
	FlowTypeStableIDs       []string
	AllocationTypeStableIDs []string
	ReachableStableIDs      []string
	RootStableIDs           []string
	RootPolicy              string
	RootPolicyVersion       string
	RootCompleteness        string
	RootBoundary            string
}

type resolutionV2Publication struct {
	CallsiteID        string
	RepoID            string
	FileNodeID        int64
	ASTPath           string
	ByteStart         uint64
	ByteEnd           uint64
	DispatchForm      string
	ParseState        string
	CandidateState    string
	SelectedTargetID  string
	SelectionRuleID   string
	DerivationSetID   string
	CompletenessSetID string
	UnresolvedID      string
	UnresolvedReason  string
	EvidenceSet       string
	BlockingPassKinds []string
	Candidates        []resolutionV2Candidate
	Completeness      []resolutionV2Fact
}

func persistVTAFlowFactsTx(tx *sql.Tx, identity GraphCompletionIdentity, c *ResolutionCallsite, candidate *ResolutionCandidate) error {
	if len(candidate.FlowSourceStableIDs) == 0 && len(candidate.FlowEdgeStableIDs) == 0 {
		return nil
	}
	sources := make(map[string]ResolutionFlowFact, len(candidate.FlowSourceFacts))
	for _, fact := range candidate.FlowSourceFacts {
		if fact.Kind != "source" || fact.CallsiteID != c.CallsiteID || fact.TargetStableID != candidate.TargetStableID {
			return fmt.Errorf("candidate %s has invalid VTA source fact binding %q", candidate.TargetStableID, fact.StableID)
		}
		sources[fact.StableID] = fact
	}
	edges := make(map[string]ResolutionFlowFact, len(candidate.FlowEdgeFacts))
	for _, fact := range candidate.FlowEdgeFacts {
		if fact.Kind != "edge" || fact.CallsiteID != c.CallsiteID || fact.TargetStableID != candidate.TargetStableID {
			return fmt.Errorf("candidate %s has invalid VTA edge fact binding %q", candidate.TargetStableID, fact.StableID)
		}
		edges[fact.StableID] = fact
	}
	for _, id := range candidate.FlowSourceStableIDs {
		fact, ok := sources[id]
		if !ok {
			return fmt.Errorf("candidate %s references missing VTA source fact %q", candidate.TargetStableID, id)
		}
		if err := persistOneVTAFlowFactTx(tx, identity, c, candidate, fact, "vta_flow_source_fact"); err != nil {
			return err
		}
	}
	for _, id := range candidate.FlowEdgeStableIDs {
		fact, ok := edges[id]
		if !ok {
			return fmt.Errorf("candidate %s references missing VTA edge fact %q", candidate.TargetStableID, id)
		}
		if err := persistOneVTAFlowFactTx(tx, identity, c, candidate, fact, "vta_flow_edge_fact"); err != nil {
			return err
		}
	}
	return nil
}

func persistOneVTAFlowFactTx(tx *sql.Tx, identity GraphCompletionIdentity, c *ResolutionCallsite, candidate *ResolutionCandidate, fact ResolutionFlowFact, nodeType string) error {
	prefix := "vta_source_"
	if fact.Kind == "edge" {
		prefix = "vta_edge_"
	}
	if err := validateVTAFactID(fact.StableID, prefix); err != nil {
		return err
	}
	var existingType, existingCallsite, existingTarget, existingPayload string
	err := tx.QueryRow(`SELECT COALESCE(node_type,''), COALESCE(callsite_id,''), COALESCE(target_symbol_id,''), COALESCE(signature,'') FROM nodes WHERE stable_id=?`, fact.StableID).Scan(&existingType, &existingCallsite, &existingTarget, &existingPayload)
	if err == nil {
		if existingType != nodeType || existingCallsite != c.CallsiteID || existingTarget != candidate.TargetStableID || existingPayload != fact.Payload {
			return fmt.Errorf("VTA fact %q is bound to conflicting graph data", fact.StableID)
		}
		return nil
	}
	if err != sql.ErrNoRows {
		return fmt.Errorf("lookup VTA fact %q: %w", fact.StableID, err)
	}
	_, err = tx.Exec(`INSERT INTO nodes
		(label,name,qualified_name,file_path,signature,language,stable_id,node_type,schema_version,source_revision,producer_build_id,
		 callsite_id,target_symbol_id,pass_kind,pass_version,step_ordinal,operation,boundary_id)
		VALUES (?,?,?,?,?,?,?, ?,2,?,?,?,?, 'vta','1',0,'seed',?)`,
		nodeType, fact.StableID, fact.StableID, c.SourceFile, fact.Payload, c.Language, fact.StableID, nodeType,
		identity.RepositoryRevision, identity.BuildID, c.CallsiteID, candidate.TargetStableID, identity.RepositoryRevision)
	if err != nil {
		return fmt.Errorf("persist VTA fact %q: %w", fact.StableID, err)
	}
	return nil
}

func v2PassKind(mechanism string) string {
	switch mechanism {
	case "same_file":
		return "direct_binding"
	case "import", "import_type":
		return "import_binding"
	case "inherited", "type_flow":
		return "scope_binding"
	case "vta":
		return "vta"
	case "return_type":
		return "return_shape"
	case "verified_unique", "name_match":
		return "unique_name_fallback"
	default:
		return "legacy_unknown"
	}
}

func v2CandidateState(c *ResolutionCallsite, candidateCount int, passKind, evidenceSet, dispatchForm string) (state, selectedTargetID, selectionRuleID string) {
	staticAuthority := dispatchForm == "static" || dispatchForm == "special"
	if c.DispatchState == "unique" && candidateCount == 1 && evidenceSet == "closed" && passKind != "legacy_unknown" && staticAuthority && c.SelectedTargetStableID != nil {
		return "selected", *c.SelectedTargetStableID, ConservativeCandidatePolicy.SelectionRuleID()
	}
	switch c.DispatchState {
	case "ambiguous":
		return "ambiguous", "", ""
	case "zero", "external_unresolved":
		return "empty", "", ""
	case "dynamic":
		return "unsupported", "", ""
	case "parser_incomplete":
		return "incomplete", "", ""
	default:
		return "incomplete", "", ""
	}
}

func v2UnresolvedReason(state string, candidateCount int) string {
	switch state {
	case "ambiguous":
		if candidateCount >= 2 {
			return "ambiguous_viable_set"
		}
		return "pass_not_run"
	case "empty":
		return "no_viable_target"
	case "unsupported":
		return "unsupported_dispatch"
	case "incomplete":
		return "parse_incomplete"
	case "not_run":
		return "pass_not_run"
	default:
		return ""
	}
}

func prepareResolutionV2(identity GraphCompletionIdentity, c *ResolutionCallsite, candidates []*ResolutionCandidate) (resolutionV2Publication, error) {
	repoID := c.RepoID
	if repoID == "" {
		repoID = identity.SourceFingerprint
	}
	fileNodeID := c.FileNodeID
	if fileNodeID <= 0 {
		fileNodeID = c.SourceID
	}
	astPath := c.ASTPath
	if astPath == "" {
		astPath = "synthetic/" + strconv.Itoa(c.CallsiteOrdinal)
	}
	byteStart, byteEnd := c.ByteStart, c.ByteEnd
	if byteStart >= byteEnd {
		byteStart = uint64(max(c.SourceLine-1, 0)) * 4096
		byteEnd = byteStart + uint64(max(len(c.Callee), 1))
	}
	dispatchForm := c.DispatchForm
	if dispatchForm == "" {
		if c.DispatchState == "dynamic" {
			dispatchForm = "dynamic_name"
		} else {
			dispatchForm = "static"
		}
	}
	fileIdentity := c.FileIdentity
	if fileIdentity == "" {
		fileIdentity = canonicalResolutionID(repoID, c.SourceFile)
	}
	callsiteID, err := StableCallsiteV2ID(repoID, identity.RepositoryRevision, fileIdentity, astPath, byteStart, byteEnd, dispatchForm, c.Callee)
	if err != nil {
		return resolutionV2Publication{}, err
	}
	parseState := c.ParseState
	if parseState == "" {
		if c.DispatchState == "parser_incomplete" {
			parseState = "recovered"
		} else {
			parseState = "complete"
		}
	}
	if parseState != "complete" && parseState != "recovered" && parseState != "failed" {
		return resolutionV2Publication{}, fmt.Errorf("unknown parse state %q", parseState)
	}
	passKind := v2PassKind(c.Mechanism)
	if _, ok := derivationPassKindsV2[passKind]; !ok {
		return resolutionV2Publication{}, fmt.Errorf("unknown v2 pass kind %q", passKind)
	}
	_, evidenceSet, _, err := resolutionDerivation(c.Mechanism, c.DispatchState, len(candidates))
	if err != nil {
		return resolutionV2Publication{}, err
	}
	sortedCandidates := append([]*ResolutionCandidate(nil), candidates...)
	sort.Slice(sortedCandidates, func(i, j int) bool { return sortedCandidates[i].TargetStableID < sortedCandidates[j].TargetStableID })
	v2Candidates := make([]resolutionV2Candidate, 0, len(sortedCandidates))
	factIDs := make([]string, 0, len(sortedCandidates))
	for ordinal, candidate := range sortedCandidates {
		if candidate.TargetStableID == "" {
			return resolutionV2Publication{}, fmt.Errorf("candidate missing stable target identity")
		}
		payload := candidate.DeclaredScope + "\x00" + candidate.ReceiverType + "\x00" + candidate.ReceiverOrigin + "\x00" + candidate.ImportChain
		factID := canonicalResolutionID(callsiteID, candidate.TargetStableID, passKind, "1", strconv.Itoa(ordinal), payload)
		copyCandidate := *candidate
		copyCandidate.Ordinal = ordinal
		v2Candidates = append(v2Candidates, resolutionV2Candidate{Candidate: &copyCandidate, EdgeID: stableCandidateV2ID(callsiteID, candidate.TargetStableID), FactID: factID, PassKind: passKind})
		factIDs = append(factIDs, factID)
	}
	coverage, _ := resolutionCoverage(c)
	completeness := make([]resolutionV2Fact, 0, len(coverage))
	completenessIDs := make([]string, 0, len(coverage))
	completenessIndex := make(map[string]int)
	for _, pass := range coverage {
		mappedPass := v2PassKind(c.Mechanism)
		if pass.PassKind != "" && pass.PassKind != "dynamic_framework" {
			mappedPass = mapCoveragePassV2(pass.PassKind)
		}
		status := "partial"
		var known *int
		covered := 0
		passClosed := evidenceSet == "closed" || pass.PassKind != "vta" ||
			(pass.PassKind == "vta" && (pass.Status == "completed_match" || pass.Status == "completed_no_match"))
		if passClosed && (pass.Status == "completed_match" || pass.Status == "completed_no_match") {
			status = "closed"
			one := 1
			known, covered = &one, 1
		} else if pass.Status == "unavailable" {
			status = "unsupported"
		} else if pass.Status == "not_run" || pass.Status == "not_applicable" {
			status = "not_run"
		}
		factID := canonicalResolutionID(callsiteID, mappedPass, pass.Version, "repository", identity.RepositoryRevision)
		fact := resolutionV2Fact{ID: factID, PassKind: mappedPass, Status: status, BoundaryKind: "repository", BoundaryID: identity.RepositoryRevision, Covered: covered, Known: known, Reason: pass.Reason, CandidateStableIDs: append([]string(nil), pass.CandidateStableIDs...), FlowTypeStableIDs: append([]string(nil), pass.FlowTypeStableIDs...), AllocationTypeStableIDs: append([]string(nil), pass.AllocationTypeStableIDs...), ReachableStableIDs: append([]string(nil), pass.ReachableStableIDs...), RootStableIDs: append([]string(nil), pass.RootStableIDs...), RootPolicy: pass.RootPolicy, RootPolicyVersion: pass.RootPolicyVersion, RootCompleteness: pass.RootCompleteness, RootBoundary: pass.RootBoundary}
		if index, exists := completenessIndex[factID]; exists {
			if completenessStatusRank(fact.Status) > completenessStatusRank(completeness[index].Status) {
				completeness[index] = fact
			}
			continue
		}
		completenessIndex[factID] = len(completeness)
		completeness = append(completeness, fact)
		completenessIDs = append(completenessIDs, factID)
	}
	state, selectedTargetID, selectionRuleID := v2CandidateState(c, len(v2Candidates), passKind, evidenceSet, dispatchForm)
	unresolvedReason := v2UnresolvedReason(state, len(v2Candidates))
	if state == "incomplete" && c.DispatchState != "parser_incomplete" {
		unresolvedReason = "pass_not_run"
	}
	unresolvedID := ""
	blockingPassKinds := []string{passKind}
	switch dispatchForm {
	case "virtual", "interface":
		blockingPassKinds = []string{"cha", "rta", "vta"}
	case "dynamic_name", "reflection":
		blockingPassKinds = []string{"reflection_model"}
	case "di_lookup":
		blockingPassKinds = []string{"di_binding"}
	case "ffi":
		blockingPassKinds = []string{"ffi_contract"}
	case "function_value":
		blockingPassKinds = []string{"higher_order_flow"}
	}
	for _, blockingPass := range blockingPassKinds {
		found := false
		for _, fact := range completeness {
			if fact.PassKind == blockingPass {
				found = true
				break
			}
		}
		if found {
			continue
		}
		factID := canonicalResolutionID(callsiteID, blockingPass, "1", "repository", identity.RepositoryRevision)
		completeness = append(completeness, resolutionV2Fact{ID: factID, PassKind: blockingPass, Status: "not_run", BoundaryKind: "repository", BoundaryID: identity.RepositoryRevision})
		completenessIDs = append(completenessIDs, factID)
	}
	if unresolvedReason != "" {
		unresolvedID = canonicalResolutionID(callsiteID, unresolvedReason, strconv.Itoa(len(v2Candidates)), identity.BuildID, identity.RepositoryRevision)
	}
	return resolutionV2Publication{
		CallsiteID: callsiteID, RepoID: repoID, FileNodeID: fileNodeID, ASTPath: astPath,
		ByteStart: byteStart, ByteEnd: byteEnd, DispatchForm: dispatchForm, ParseState: parseState,
		CandidateState: state, SelectedTargetID: selectedTargetID, SelectionRuleID: selectionRuleID,
		DerivationSetID: canonicalResolutionID(factIDs...), CompletenessSetID: canonicalResolutionID(completenessIDs...),
		UnresolvedID: unresolvedID, UnresolvedReason: unresolvedReason, Candidates: v2Candidates, Completeness: completeness,
		EvidenceSet: evidenceSet, BlockingPassKinds: blockingPassKinds,
	}, nil
}

func completenessStatusRank(status string) int {
	switch status {
	case "closed":
		return 4
	case "unsupported":
		return 3
	case "partial":
		return 2
	case "not_run":
		return 1
	default:
		return 0
	}
}

func mapCoveragePassV2(pass string) string {
	switch pass {
	// overload_narrowing and mro are projections, not derivations: they filter
	// and order a candidate set some other pass produced. They pass through
	// under their own names so the v2 fact says which projection ran, rather
	// than collapsing to legacy_unknown and losing the distinction.
	case "direct_binding", "cha", "rta", "vta", "points_to_field_insensitive", "points_to_field_sensitive", "on_the_fly_refinement", "framework_route", "di_binding", "reflection_model", "higher_order_flow", "ffi_contract", "generated_mapping", "overload_narrowing", "mro":
		return pass
	case "lexical_binding":
		return "direct_binding"
	case "import_binding":
		return "import_binding"
	case "declared_type", "implementation_set":
		return "scope_binding"
	case "return_type":
		return "return_shape"
	case "global_name":
		return "unique_name_fallback"
	default:
		return "legacy_unknown"
	}
}

func insertResolutionPolicyV2Tx(tx *sql.Tx, policy CandidateQueryPolicy, identity GraphCompletionIdentity) error {
	policyID := policy.VersionID()
	policyJSON := policy.CanonicalJSON()
	_, err := tx.Exec(`INSERT OR IGNORE INTO nodes
		(label,name,qualified_name,file_path,language,stable_id,node_type,schema_version,source_revision,producer_build_id,policy_name,semantic_version,policy_json,policy_hash,
		 created_at,created_by,fact_schema_min,fact_schema_max,explanation_template_version,active_from,supersedes_id)
		VALUES ('QueryPolicyVersion',?,?,?,?,?,'query_policy_version',2,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		policy.ID, policyID, "", "policy", policyID, identity.RepositoryRevision, identity.BuildID, policy.ID, policy.Version, policyJSON, policyID,
		identity.BuildTimeUTC, "gt-index", 2, 2, "1", nil, nil)
	return err
}

func insertResolutionV2FactsTx(tx *sql.Tx, identity GraphCompletionIdentity, c *ResolutionCallsite, callsiteNodeID int64, v2 resolutionV2Publication) error {
	for _, candidate := range v2.Candidates {
		fact := candidate.Candidate
		if err := persistVTAFlowFactsTx(tx, identity, c, fact); err != nil {
			return fmt.Errorf("persist VTA flow facts for %s/%d: %w", c.CallsiteID, fact.Ordinal, err)
		}
		declaredTypeID := ""
		if fact.ReceiverType != "" {
			declaredTypeID = canonicalResolutionID("type", fact.ReceiverType)
		}
		scopeID := ""
		if fact.DeclaredScope != "" {
			scopeID = canonicalResolutionID("scope", fact.DeclaredScope)
		}
		importID := ""
		if fact.ImportChain != "" && fact.ImportChain != "[]" {
			importID = canonicalResolutionID("import", fact.ImportChain)
		}
		result, err := tx.Exec(`INSERT INTO nodes
			(label,name,qualified_name,file_path,language,stable_id,node_type,schema_version,source_revision,producer_build_id,
			 callsite_id,target_symbol_id,pass_kind,pass_version,step_ordinal,operation,input_fact_ids,declared_type_id,scope_id,import_id,boundary_id)
			VALUES ('DerivationFact',?,?,?,?,?,'derivation_fact',2,?,?,?,?,?,'1',0,'include',?,?,?,?,?)`,
			candidate.PassKind, candidate.FactID, c.SourceFile, "resolution", candidate.FactID,
			identity.RepositoryRevision, identity.BuildID, v2.CallsiteID, fact.TargetStableID, candidate.PassKind,
			mustJSON(append(append([]string{}, fact.FlowSourceStableIDs...), fact.FlowEdgeStableIDs...)), declaredTypeID, scopeID, importID, identity.RepositoryRevision)
		if err != nil {
			return fmt.Errorf("insert derivation fact: %w", err)
		}
		factNodeID, err := result.LastInsertId()
		if err != nil {
			return err
		}
		if err := insertResolutionFactLinkTx(tx, callsiteNodeID, factNodeID, "HAS_DERIVATION_FACT", v2.CallsiteID, candidate.FactID); err != nil {
			return err
		}
		if _, err := tx.Exec(`INSERT INTO edges
			(source_id,target_id,type,source_file,resolution_method,confidence,trust_tier,candidate_count,evidence_type,verification_status,
			 stable_id,schema_version,callsite_stable_id,target_symbol_id,ordinal,viability,derivation_fact_ids,exclusion_fact_ids,
			 derivation_contract,derivation_kind,evidence_set,analysis_boundary,pass_kind,pass_version,pass_status,sibling_count,producer_build_id,producer_source_fingerprint,
			 declared_scope,receiver_type,receiver_origin,receiver_shape,receiver_chain,import_chain,export_status,parser_complete)
			VALUES (?,?, 'CANDIDATE_TARGET',?,?,NULL,'CANDIDATE',?,'resolver_candidate',?,?,?,?,? ,?,'viable',?,'[]',?,?,?,?,?,'1','completed',?,?,?,?,?,?,?,?,?,?,?)`,
			callsiteNodeID, fact.TargetID, c.SourceFile, fact.Mechanism, len(v2.Candidates), fact.VerificationStatus,
			candidate.EdgeID, CallResolutionSchemaVersion, v2.CallsiteID, fact.TargetStableID, fact.Ordinal,
			mustJSON([]string{candidate.FactID}), ResolutionDerivationContract, candidate.PassKind, v2.EvidenceSet, identity.RepositoryRevision,
			candidate.PassKind, len(v2.Candidates), identity.BuildID, identity.SourceFingerprint,
			fact.DeclaredScope, fact.ReceiverType, fact.ReceiverOrigin, fact.ReceiverShape, fact.ReceiverChain,
			fact.ImportChain, fact.ExportStatus, fact.ParserComplete); err != nil {
			return fmt.Errorf("insert v2 candidate edge: %w", err)
		}
	}
	if v2.CandidateState == "selected" {
		var selected *ResolutionCandidate
		for _, candidate := range v2.Candidates {
			if candidate.Candidate.TargetStableID == v2.SelectedTargetID {
				selected = candidate.Candidate
				break
			}
		}
		if selected == nil {
			return fmt.Errorf("selected v2 target is not a viable candidate")
		}
		selectionID := canonicalResolutionID(v2.CallsiteID, "SELECTED_TARGET", v2.SelectedTargetID, v2.SelectionRuleID)
		if _, err := tx.Exec(`INSERT INTO edges
			(source_id,target_id,type,source_line,source_file,resolution_method,confidence,trust_tier,candidate_count,evidence_type,verification_status,
			 stable_id,schema_version,callsite_stable_id,target_symbol_id,selection_rule_id,analysis_boundary,producer_build_id,producer_source_fingerprint)
			VALUES (?,?, 'SELECTED_TARGET',?,?,?,NULL,'CERTIFIED',1,'call_resolution_v2','source_supported',?,2,?,?,?,?,?,?)`,
			callsiteNodeID, selected.TargetID, c.SourceLine, c.SourceFile, selected.Mechanism, selectionID, v2.CallsiteID,
			v2.SelectedTargetID, v2.SelectionRuleID, identity.RepositoryRevision, identity.BuildID, identity.SourceFingerprint); err != nil {
			return fmt.Errorf("insert selected v2 target edge: %w", err)
		}
	}
	for _, fact := range v2.Completeness {
		var known any
		if fact.Known != nil {
			known = *fact.Known
		}
		result, err := tx.Exec(`INSERT INTO nodes
			(label,name,qualified_name,file_path,language,stable_id,node_type,schema_version,source_revision,producer_build_id,
			 callsite_id,pass_kind,pass_version,boundary_kind,boundary_id,fact_status,covered_units,known_units,reason_code,candidate_stable_ids,flow_type_stable_ids,allocation_type_stable_ids,reachable_stable_ids,root_stable_ids,root_policy,root_policy_version,root_completeness,root_boundary)
			VALUES ('CompletenessFact',?,?,?,?,?,'completeness_fact',2,?,?,?,?,'1',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
			fact.PassKind, fact.ID, c.SourceFile, "resolution", fact.ID, identity.RepositoryRevision, identity.BuildID,
			v2.CallsiteID, fact.PassKind, fact.BoundaryKind, fact.BoundaryID, fact.Status, fact.Covered, known, fact.Reason, mustJSON(fact.CandidateStableIDs), mustJSON(fact.FlowTypeStableIDs), mustJSON(fact.AllocationTypeStableIDs), mustJSON(fact.ReachableStableIDs), mustJSON(fact.RootStableIDs), fact.RootPolicy, fact.RootPolicyVersion, fact.RootCompleteness, fact.RootBoundary)
		if err != nil {
			return fmt.Errorf("insert completeness fact: %w", err)
		}
		factNodeID, err := result.LastInsertId()
		if err != nil {
			return err
		}
		if err := insertResolutionFactLinkTx(tx, callsiteNodeID, factNodeID, "HAS_COMPLETENESS_FACT", v2.CallsiteID, fact.ID); err != nil {
			return err
		}
	}
	if v2.UnresolvedID != "" {
		result, err := tx.Exec(`INSERT INTO nodes
			(label,name,qualified_name,file_path,language,stable_id,node_type,schema_version,source_revision,producer_build_id,
			 callsite_id,reason_code,candidate_count_v2,blocking_pass_kinds)
			VALUES ('UnresolvedFact',?,?,?,?,?,'unresolved_fact',2,?,?,?,?,?,?)`,
			v2.UnresolvedReason, v2.UnresolvedID, c.SourceFile, "resolution", v2.UnresolvedID,
			identity.RepositoryRevision, identity.BuildID, v2.CallsiteID, v2.UnresolvedReason, len(v2.Candidates), mustJSON(v2.BlockingPassKinds))
		if err != nil {
			return fmt.Errorf("insert unresolved fact: %w", err)
		}
		factNodeID, err := result.LastInsertId()
		if err != nil {
			return err
		}
		if err := insertResolutionFactLinkTx(tx, callsiteNodeID, factNodeID, "HAS_UNRESOLVED_FACT", v2.CallsiteID, v2.UnresolvedID); err != nil {
			return err
		}
	}
	return nil
}

func insertResolutionFactLinkTx(tx *sql.Tx, callsiteNodeID, factNodeID int64, edgeType, callsiteID, factID string) error {
	edgeID := canonicalResolutionID(callsiteID, edgeType, factID)
	_, err := tx.Exec(`INSERT INTO edges
		(source_id,target_id,type,source_line,source_file,confidence,trust_tier,evidence_type,verification_status,stable_id,schema_version,callsite_stable_id)
		VALUES (?,?,?,0,'',NULL,'STRUCTURAL','resolution_fact','structural_only',?,2,?)`,
		callsiteNodeID, factNodeID, edgeType, edgeID, callsiteID)
	if err != nil {
		return fmt.Errorf("insert %s link: %w", edgeType, err)
	}
	return nil
}

func mustJSON(value any) string {
	encoded, err := json.Marshal(value)
	if err != nil {
		panic(err)
	}
	return string(encoded)
}

func nullableString(value string) any {
	if value == "" {
		return nil
	}
	return value
}

func (d *DB) loadResolutionV2Evidence(candidates []AttachedCandidate) error {
	coverageByCallsite := make(map[string][]ResolutionPassCoverage)
	for i := range candidates {
		callsiteID := candidates[i].CallsiteID
		coverage, ok := coverageByCallsite[callsiteID]
		if !ok {
			rows, err := d.db.Query(`SELECT stable_id,pass_kind,pass_version,fact_status,COALESCE(reason_code,''),COALESCE(candidate_stable_ids,'[]'),COALESCE(flow_type_stable_ids,'[]'),COALESCE(allocation_type_stable_ids,'[]'),COALESCE(reachable_stable_ids,'[]'),COALESCE(root_stable_ids,'[]'),COALESCE(root_policy,''),COALESCE(root_policy_version,''),COALESCE(root_completeness,''),COALESCE(root_boundary,'') FROM nodes
				WHERE node_type='completeness_fact' AND callsite_id=? ORDER BY pass_kind,stable_id`, callsiteID)
			if err != nil {
				return fmt.Errorf("load completeness facts: %w", err)
			}
			for rows.Next() {
				var fact ResolutionPassCoverage
				var candidateIDsJSON, flowTypeIDsJSON, allocationTypeIDsJSON, reachableIDsJSON, rootIDsJSON string
				if err := rows.Scan(&fact.FactID, &fact.PassKind, &fact.Version, &fact.Status, &fact.Reason, &candidateIDsJSON, &flowTypeIDsJSON, &allocationTypeIDsJSON, &reachableIDsJSON, &rootIDsJSON, &fact.RootPolicy, &fact.RootPolicyVersion, &fact.RootCompleteness, &fact.RootBoundary); err != nil {
					rows.Close()
					return err
				}
				if err := json.Unmarshal([]byte(candidateIDsJSON), &fact.CandidateStableIDs); err != nil {
					rows.Close()
					return fmt.Errorf("decode candidate stable IDs for %s: %w", fact.FactID, err)
				}
				if err := json.Unmarshal([]byte(flowTypeIDsJSON), &fact.FlowTypeStableIDs); err != nil {
					rows.Close()
					return fmt.Errorf("decode flow type stable IDs for %s: %w", fact.FactID, err)
				}
				if err := json.Unmarshal([]byte(allocationTypeIDsJSON), &fact.AllocationTypeStableIDs); err != nil {
					rows.Close()
					return fmt.Errorf("decode allocation type stable IDs for %s: %w", fact.FactID, err)
				}
				if err := json.Unmarshal([]byte(reachableIDsJSON), &fact.ReachableStableIDs); err != nil {
					rows.Close()
					return fmt.Errorf("decode reachable stable IDs for %s: %w", fact.FactID, err)
				}
				if err := json.Unmarshal([]byte(rootIDsJSON), &fact.RootStableIDs); err != nil {
					rows.Close()
					return fmt.Errorf("decode root stable IDs for %s: %w", fact.FactID, err)
				}
				coverage = append(coverage, fact)
			}
			if err := rows.Close(); err != nil {
				return err
			}
			coverageByCallsite[callsiteID] = coverage
		}
		candidates[i].PassCoverage = append([]ResolutionPassCoverage(nil), coverage...)
		candidates[i].CompletenessStates = make(map[string]string, len(coverage))
		for _, fact := range coverage {
			candidates[i].FactIDsRead = append(candidates[i].FactIDsRead, fact.FactID)
			candidates[i].CompletenessStates[fact.PassKind] = fact.Status
		}
		rows, err := d.db.Query(`SELECT stable_id,pass_kind,pass_version,step_ordinal,operation,
			COALESCE(input_fact_ids,'[]'),COALESCE(declared_type_id,''),COALESCE(receiver_value_id,''),COALESCE(scope_id,''),COALESCE(import_id,''),COALESCE(boundary_id,'')
			FROM nodes WHERE node_type='derivation_fact' AND callsite_id=? AND target_symbol_id=?
			ORDER BY pass_kind,step_ordinal,stable_id`, callsiteID, candidates[i].TargetStableID)
		if err != nil {
			return fmt.Errorf("load derivation facts: %w", err)
		}
		for rows.Next() {
			var id, passKind, passVersion, operation, inputFactIDsJSON, declaredTypeID, receiverValueID, scopeID, importID, boundaryID string
			var ordinal int
			if err := rows.Scan(&id, &passKind, &passVersion, &ordinal, &operation, &inputFactIDsJSON, &declaredTypeID, &receiverValueID, &scopeID, &importID, &boundaryID); err != nil {
				rows.Close()
				return err
			}
			var inputFactIDs []string
			if err := json.Unmarshal([]byte(inputFactIDsJSON), &inputFactIDs); err != nil {
				rows.Close()
				return fmt.Errorf("decode derivation input facts for %s: %w", id, err)
			}
			for _, inputFactID := range inputFactIDs {
				switch {
				case len(inputFactID) > len("vta_source_") && inputFactID[:len("vta_source_")] == "vta_source_":
					candidates[i].FlowSourceStableIDs = appendUniqueString(candidates[i].FlowSourceStableIDs, inputFactID)
					var nodeType, factCallsite, factTarget, payload string
					if err := d.db.QueryRow(`SELECT COALESCE(node_type,''), COALESCE(callsite_id,''), COALESCE(target_symbol_id,''), COALESCE(signature,'') FROM nodes WHERE stable_id=?`, inputFactID).Scan(&nodeType, &factCallsite, &factTarget, &payload); err != nil {
						rows.Close()
						return fmt.Errorf("load VTA source fact %s: %w", inputFactID, err)
					}
					if nodeType != "vta_flow_source_fact" || factTarget != candidates[i].TargetStableID {
						rows.Close()
						return fmt.Errorf("VTA source fact %s is not bound to candidate %s (node type=%q node_callsite=%q candidate_callsite=%q target=%q)", inputFactID, candidates[i].TargetStableID, nodeType, factCallsite, candidates[i].CallsiteID, factTarget)
					}
					candidates[i].FlowSourceFacts = append(candidates[i].FlowSourceFacts, ResolutionFlowFact{StableID: inputFactID, Kind: "source", CallsiteID: factCallsite, TargetStableID: factTarget, Payload: payload})
				case len(inputFactID) > len("vta_edge_") && inputFactID[:len("vta_edge_")] == "vta_edge_":
					candidates[i].FlowEdgeStableIDs = appendUniqueString(candidates[i].FlowEdgeStableIDs, inputFactID)
					var nodeType, factCallsite, factTarget, payload string
					if err := d.db.QueryRow(`SELECT COALESCE(node_type,''), COALESCE(callsite_id,''), COALESCE(target_symbol_id,''), COALESCE(signature,'') FROM nodes WHERE stable_id=?`, inputFactID).Scan(&nodeType, &factCallsite, &factTarget, &payload); err != nil {
						rows.Close()
						return fmt.Errorf("load VTA edge fact %s: %w", inputFactID, err)
					}
					if nodeType != "vta_flow_edge_fact" || factTarget != candidates[i].TargetStableID {
						rows.Close()
						return fmt.Errorf("VTA edge fact %s is not bound to candidate %s", inputFactID, candidates[i].TargetStableID)
					}
					candidates[i].FlowEdgeFacts = append(candidates[i].FlowEdgeFacts, ResolutionFlowFact{StableID: inputFactID, Kind: "edge", CallsiteID: factCallsite, TargetStableID: factTarget, Payload: payload})
				}
			}
			payload := mustJSON(map[string]any{"boundary_id": boundaryID, "declared_type_id": declaredTypeID, "import_id": importID, "input_fact_ids": inputFactIDs, "operation": operation, "pass_kind": passKind, "pass_version": passVersion, "receiver_value_id": receiverValueID, "scope_id": scopeID})
			candidates[i].ProvenanceSteps = append(candidates[i].ProvenanceSteps, ResolutionDerivationStep{StepID: id, Kind: passKind, Value: payload})
			candidates[i].FactIDsRead = append(candidates[i].FactIDsRead, id)
		}
		if err := rows.Close(); err != nil {
			return err
		}
	}
	return nil
}

func appendUniqueString(values []string, value string) []string {
	for _, existing := range values {
		if existing == value {
			return values
		}
	}
	return append(values, value)
}

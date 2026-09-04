package store

import (
	"database/sql"
	"fmt"
)

// resolutionStmts is the per-transaction statement set used by the resolution
// publication. Every insert below runs once per callsite, once per candidate or
// once per completeness fact, so on a repository with tens of thousands of
// callsites the publication used to re-parse the same handful of statements
// hundreds of thousands of times. Preparing them once is a pure performance
// change: the SQL text and the bound arguments are unchanged.
//
// vtaFacts is the other half of the same problem. persistOneVTAFlowFactTx ran a
// SELECT before every insert, per candidate and per flow ID. The existence
// check is hoisted here so a fact already published in this transaction costs a
// map lookup. The cached binding is compared exactly as the SELECT result was,
// so a conflicting rebinding is still rejected rather than silently accepted.
type resolutionStmts struct {
	callsiteNode        *sql.Stmt
	hasCallsiteEdge     *sql.Stmt
	candidateEdge       *sql.Stmt
	derivationFact      *sql.Stmt
	candidateTargetEdge *sql.Stmt
	selectedTargetEdge  *sql.Stmt
	completenessFact    *sql.Stmt
	unresolvedFact      *sql.Stmt
	factLink            *sql.Stmt
	vtaFactLookup       *sql.Stmt
	vtaFactInsert       *sql.Stmt

	vtaFacts map[string]vtaFactBinding
}

// emptyCoverageProjection is what a candidate edge stores in place of a copy of
// its callsite's pass coverage. The empty JSON array is exactly what
// queryAttachedCandidates already substitutes for that column, so the value a
// reader observes is unchanged; only the duplication is gone.
const emptyCoverageProjection = "[]"

// vtaFactBinding is the identity a VTA flow fact was published with. It is the
// same tuple the pre-insert SELECT read back, kept in memory so the comparison
// does not require a round trip.
type vtaFactBinding struct {
	NodeType   string
	CallsiteID string
	TargetID   string
	Payload    string
}

const (
	callsiteNodeSQL = `INSERT INTO nodes
			(label,name,qualified_name,file_path,start_line,end_line,signature,language,
			 stable_id,node_type,schema_version,repo_id,source_revision,producer_build_id,file_node_id,caller_symbol_id,
			 ast_path,byte_start,byte_end,line_start,column_start,dispatch_form,callee_lexeme,argument_arity,parse_state,
			 candidate_state,selected_target_id,candidate_count_v2,derivation_set_id,completeness_set_id)
			VALUES ('Callsite',?,?,?,?,?,?,?,?, 'callsite',2,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`

	hasCallsiteEdgeSQL = `INSERT INTO edges
			(source_id,target_id,type,source_line,source_file,resolution_method,confidence,metadata,trust_tier,candidate_count,evidence_type,verification_status,stable_id,schema_version,callsite_stable_id,
			 derivation_contract,derivation_kind,evidence_set,analysis_boundary,pass_kind,pass_version,pass_status,abstention_reason,sibling_count,
			 producer_build_id,producer_source_fingerprint,pass_coverage,provenance_steps)
			VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`

	candidateEdgeSQL = `INSERT INTO edges
				(source_id,target_id,type,source_line,source_file,resolution_method,confidence,metadata,trust_tier,candidate_count,evidence_type,verification_status,
				 derivation_contract,derivation_kind,evidence_set,analysis_boundary,pass_kind,pass_version,pass_status,abstention_reason,sibling_count,
				 producer_build_id,producer_source_fingerprint,pass_coverage,provenance_steps,
				 declared_scope,receiver_type,receiver_origin,receiver_shape,receiver_chain,import_chain,export_status,parser_complete)
				VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`

	derivationFactSQL = `INSERT INTO nodes
			(label,name,qualified_name,file_path,language,stable_id,node_type,schema_version,source_revision,producer_build_id,
			 callsite_id,target_symbol_id,pass_kind,pass_version,step_ordinal,operation,input_fact_ids,declared_type_id,scope_id,import_id,boundary_id)
			VALUES ('DerivationFact',?,?,?,?,?,'derivation_fact',2,?,?,?,?,?,'1',0,'include',?,?,?,?,?)`

	candidateTargetEdgeSQL = `INSERT INTO edges
			(source_id,target_id,type,source_file,resolution_method,confidence,trust_tier,candidate_count,evidence_type,verification_status,
			 stable_id,schema_version,callsite_stable_id,target_symbol_id,ordinal,viability,derivation_fact_ids,exclusion_fact_ids,
			 derivation_contract,derivation_kind,evidence_set,analysis_boundary,pass_kind,pass_version,pass_status,sibling_count,producer_build_id,producer_source_fingerprint,
			 declared_scope,receiver_type,receiver_origin,receiver_shape,receiver_chain,import_chain,export_status,parser_complete)
			VALUES (?,?, 'CANDIDATE_TARGET',?,?,NULL,'CANDIDATE',?,'resolver_candidate',?,?,?,?,? ,?,'viable',?,'[]',?,?,?,?,?,'1','completed',?,?,?,?,?,?,?,?,?,?,?)`

	selectedTargetEdgeSQL = `INSERT INTO edges
			(source_id,target_id,type,source_line,source_file,resolution_method,confidence,trust_tier,candidate_count,evidence_type,verification_status,
			 stable_id,schema_version,callsite_stable_id,target_symbol_id,selection_rule_id,analysis_boundary,producer_build_id,producer_source_fingerprint)
			VALUES (?,?, 'SELECTED_TARGET',?,?,?,NULL,'CERTIFIED',1,'call_resolution_v2','source_supported',?,2,?,?,?,?,?,?)`

	completenessFactSQL = `INSERT INTO nodes
			(label,name,qualified_name,file_path,language,stable_id,node_type,schema_version,source_revision,producer_build_id,
			 callsite_id,pass_kind,pass_version,boundary_kind,boundary_id,fact_status,covered_units,known_units,reason_code,candidate_stable_ids,flow_type_stable_ids,allocation_type_stable_ids,reachable_stable_ids,root_stable_ids,root_policy,root_policy_version,root_completeness,root_boundary)
			VALUES ('CompletenessFact',?,?,?,?,?,'completeness_fact',2,?,?,?,?,'1',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`

	unresolvedFactSQL = `INSERT INTO nodes
			(label,name,qualified_name,file_path,language,stable_id,node_type,schema_version,source_revision,producer_build_id,
			 callsite_id,reason_code,candidate_count_v2,blocking_pass_kinds)
			VALUES ('UnresolvedFact',?,?,?,?,?,'unresolved_fact',2,?,?,?,?,?,?)`

	factLinkSQL = `INSERT INTO edges
		(source_id,target_id,type,source_line,source_file,confidence,trust_tier,evidence_type,verification_status,stable_id,schema_version,callsite_stable_id)
		VALUES (?,?,?,0,'',NULL,'STRUCTURAL','resolution_fact','structural_only',?,2,?)`

	vtaFactLookupSQL = `SELECT COALESCE(node_type,''), COALESCE(callsite_id,''), COALESCE(target_symbol_id,''), COALESCE(signature,'') FROM nodes WHERE stable_id=?`

	vtaFactInsertSQL = `INSERT INTO nodes
		(label,name,qualified_name,file_path,signature,language,stable_id,node_type,schema_version,source_revision,producer_build_id,
		 callsite_id,target_symbol_id,pass_kind,pass_version,step_ordinal,operation,boundary_id)
		VALUES (?,?,?,?,?,?,?, ?,2,?,?,?,?, 'vta','1',0,'seed',?)`
)

func prepareResolutionStmts(tx *sql.Tx) (*resolutionStmts, error) {
	stmts := &resolutionStmts{vtaFacts: make(map[string]vtaFactBinding)}
	for _, binding := range []struct {
		name   string
		query  string
		target **sql.Stmt
	}{
		{"callsite node", callsiteNodeSQL, &stmts.callsiteNode},
		{"has-callsite edge", hasCallsiteEdgeSQL, &stmts.hasCallsiteEdge},
		{"candidate edge", candidateEdgeSQL, &stmts.candidateEdge},
		{"derivation fact", derivationFactSQL, &stmts.derivationFact},
		{"candidate target edge", candidateTargetEdgeSQL, &stmts.candidateTargetEdge},
		{"selected target edge", selectedTargetEdgeSQL, &stmts.selectedTargetEdge},
		{"completeness fact", completenessFactSQL, &stmts.completenessFact},
		{"unresolved fact", unresolvedFactSQL, &stmts.unresolvedFact},
		{"resolution fact link", factLinkSQL, &stmts.factLink},
		{"vta fact lookup", vtaFactLookupSQL, &stmts.vtaFactLookup},
		{"vta fact insert", vtaFactInsertSQL, &stmts.vtaFactInsert},
	} {
		prepared, err := tx.Prepare(binding.query)
		if err != nil {
			stmts.Close()
			return nil, fmt.Errorf("prepare %s statement: %w", binding.name, err)
		}
		*binding.target = prepared
	}
	return stmts, nil
}

// Close releases the prepared statements. It is safe on a partially prepared
// set, which is what the constructor unwinds on failure.
func (s *resolutionStmts) Close() {
	if s == nil {
		return
	}
	for _, stmt := range []*sql.Stmt{
		s.callsiteNode, s.hasCallsiteEdge, s.candidateEdge, s.derivationFact,
		s.candidateTargetEdge, s.selectedTargetEdge, s.completenessFact,
		s.unresolvedFact, s.factLink, s.vtaFactLookup, s.vtaFactInsert,
	} {
		if stmt != nil {
			_ = stmt.Close()
		}
	}
}

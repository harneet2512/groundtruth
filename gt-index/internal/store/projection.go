package store

import (
	"database/sql"
	"fmt"
	"strings"
)

// ResolutionPassOrder is the producer's single statement of the order in which
// resolution passes run. The resolver's tracker, the store's compatibility
// coverage fallback and the `step` projection all read this one slice, so a
// step ordinal can never drift from the pass order that produced it.
var ResolutionPassOrder = []string{
	"lexical_binding", "import_binding", "declared_type", "implementation_set",
	"return_type", "global_name", "dynamic_framework",
}

// ResolutionStepUnmapped is the step of an edge whose winning pass_kind is not
// one of the ordered passes -- a derivation kind stood in for a pass that never
// reported. It is negative so a consumer cannot mistake it for a real ordinal,
// and the projected reason names it in words as well.
const ResolutionStepUnmapped = -1

// ResolutionProjectionVersion versions the rendered `reason` text. The text is
// derived, so a consumer that caches it needs to know when the rendering
// changed even though the underlying facts did not.
const ResolutionProjectionVersion = "gt-index.resolution-projection.v1"

// ResolutionStepOrdinal projects the 1-based position of a pass in
// ResolutionPassOrder. It reports false for anything that is not an ordered
// pass, which the caller must publish as unmapped rather than as step zero.
func ResolutionStepOrdinal(passKind string) (int, bool) {
	for index, pass := range ResolutionPassOrder {
		if pass == passKind {
			return index + 1, true
		}
	}
	return ResolutionStepUnmapped, false
}

// ResolutionReason renders the justification for one resolution edge from the
// derivation facts already stored on it. Every clause is a projection of a
// column: nothing here is written by hand alongside the edge, so the text
// cannot drift from what produced it.
//
// derivationKind and evidenceSet are resolutionDerivation's own output;
// abstentionReason is its abstention or the producer's budget boundary;
// passKind is the pass that matched.
func ResolutionReason(derivationKind, evidenceSet, abstentionReason, passKind string, candidateCount int) string {
	if derivationKind == "" && passKind == "" {
		return ""
	}
	clauses := []string{
		"step " + renderStep(passKind),
		renderDerivation(derivationKind, candidateCount),
		"evidence set " + orUnstated(evidenceSet),
		renderAbstention(abstentionReason),
	}
	return strings.Join(clauses, "; ")
}

func renderStep(passKind string) string {
	ordinal, mapped := ResolutionStepOrdinal(passKind)
	if !mapped {
		return "unmapped (" + orUnstated(passKind) + ")"
	}
	return fmt.Sprintf("%d of %d (%s)", ordinal, len(ResolutionPassOrder), passKind)
}

func renderDerivation(derivationKind string, candidateCount int) string {
	noun := "candidates"
	if candidateCount == 1 {
		noun = "candidate"
	}
	return fmt.Sprintf("%s derivation over %d %s", orUnstated(derivationKind), candidateCount, noun)
}

func renderAbstention(abstentionReason string) string {
	if abstentionReason == "" {
		return "no abstention"
	}
	return "abstained: " + abstentionReason
}

func orUnstated(value string) string {
	if value == "" {
		return "unstated"
	}
	return value
}

// ResolutionProjectionReport counts what one projection pass wrote. A build
// reports these so "the reason column is populated" is a measurement rather
// than a claim.
type ResolutionProjectionReport struct {
	DistinctFacts    int
	CallsiteEdges    int
	CandidateEdges   int
	UnmappedStepRows int
}

// projectionEdgeTypes are the resolution edges that carry a derivation and
// therefore a reason. Every other edge type keeps a NULL reason, which reads as
// "this edge is not a resolution decision" rather than as a missing value.
var projectionEdgeTypes = []string{"HAS_CALLSITE", "CANDIDATE"}

// ProjectResolutionEdgeReasonsTx renders `reason` and `step` onto every
// resolution edge from the derivation facts already stored on it.
//
// It runs after attachment rather than inside it because a projection must be
// reproducible from the published graph alone: anything it needs that is not
// already a column would be a fact the projection invented. Grouping by the
// distinct derivation tuple keeps it a handful of statements instead of one per
// edge.
func ProjectResolutionEdgeReasonsTx(tx *sql.Tx) (ResolutionProjectionReport, error) {
	report := ResolutionProjectionReport{}
	if tx == nil {
		return report, fmt.Errorf("nil projection transaction")
	}
	facts, err := distinctDerivationFacts(tx)
	if err != nil {
		return report, err
	}
	update, err := tx.Prepare(`UPDATE edges SET resolution_reason=?, resolution_step=?
		WHERE type=? AND ifnull(derivation_kind,'')=? AND ifnull(evidence_set,'')=?
		  AND ifnull(abstention_reason,'')=? AND ifnull(pass_kind,'')=? AND ifnull(candidate_count,0)=?`)
	if err != nil {
		return report, fmt.Errorf("prepare resolution projection: %w", err)
	}
	defer update.Close()

	for _, fact := range facts {
		reason := ResolutionReason(fact.derivationKind, fact.evidenceSet, fact.abstentionReason, fact.passKind, fact.candidateCount)
		step, mapped := ResolutionStepOrdinal(fact.passKind)
		result, err := update.Exec(reason, step, fact.edgeType, fact.derivationKind, fact.evidenceSet,
			fact.abstentionReason, fact.passKind, fact.candidateCount)
		if err != nil {
			return report, fmt.Errorf("project resolution reason for %s: %w", fact.edgeType, err)
		}
		affected, err := result.RowsAffected()
		if err != nil {
			return report, fmt.Errorf("count projected resolution rows: %w", err)
		}
		report.DistinctFacts++
		if !mapped {
			report.UnmappedStepRows += int(affected)
		}
		if fact.edgeType == "HAS_CALLSITE" {
			report.CallsiteEdges += int(affected)
			continue
		}
		report.CandidateEdges += int(affected)
	}
	return report, nil
}

type derivationFact struct {
	edgeType         string
	derivationKind   string
	evidenceSet      string
	abstentionReason string
	passKind         string
	candidateCount   int
}

func distinctDerivationFacts(tx *sql.Tx) ([]derivationFact, error) {
	var facts []derivationFact
	for _, edgeType := range projectionEdgeTypes {
		rows, err := tx.Query(`SELECT DISTINCT ifnull(derivation_kind,''), ifnull(evidence_set,''),
			ifnull(abstention_reason,''), ifnull(pass_kind,''), ifnull(candidate_count,0)
			FROM edges WHERE type=?`, edgeType)
		if err != nil {
			return nil, fmt.Errorf("read %s derivation facts: %w", edgeType, err)
		}
		typeFacts, err := scanDerivationFacts(rows, edgeType)
		if err != nil {
			return nil, err
		}
		facts = append(facts, typeFacts...)
	}
	return facts, nil
}

func scanDerivationFacts(rows *sql.Rows, edgeType string) ([]derivationFact, error) {
	defer rows.Close()
	var facts []derivationFact
	for rows.Next() {
		fact := derivationFact{edgeType: edgeType}
		if err := rows.Scan(&fact.derivationKind, &fact.evidenceSet, &fact.abstentionReason,
			&fact.passKind, &fact.candidateCount); err != nil {
			return nil, fmt.Errorf("scan %s derivation fact: %w", edgeType, err)
		}
		facts = append(facts, fact)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate %s derivation facts: %w", edgeType, err)
	}
	return facts, nil
}

// ProjectResolutionEdgeReasons is the non-transactional wrapper for callers
// that own no publication transaction.
func (d *DB) ProjectResolutionEdgeReasons() (ResolutionProjectionReport, error) {
	tx, err := d.db.Begin()
	if err != nil {
		return ResolutionProjectionReport{}, fmt.Errorf("begin resolution projection tx: %w", err)
	}
	report, err := ProjectResolutionEdgeReasonsTx(tx)
	if err != nil {
		_ = tx.Rollback()
		return ResolutionProjectionReport{}, err
	}
	if err := tx.Commit(); err != nil {
		return ResolutionProjectionReport{}, fmt.Errorf("commit resolution projection: %w", err)
	}
	return report, nil
}

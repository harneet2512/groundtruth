package community

import (
	"context"
	"database/sql"
	"fmt"
	"sort"
	"strconv"
	"strings"
)

// Evidence rebinding for a carried partition.
//
// A batch amend may carry the parent's communities untouched when the history
// window and the certified cross-file call multiset (CertifiedCallGraphDigest)
// both match. The partition is then provably the partition a rebuild would
// find, but its evidence list is not: a CERTIFIED CALLS contribution is cited
// by its decimal edges.id, and the amend re-inserts every edge, so the carried
// ids name rows the published graph no longer has.
//
// Because the digest matched, the per-pair call COUNTS are unchanged, so the
// evidence list a rebuild would write has exactly the carried list's shape:
// the same pairs in the same order, the same number of call ids per pair, the
// same co-change markers at the same positions. ReboundCallEvidence rewrites
// only the call ids, and refuses (error) whenever that shape does not hold,
// so the caller falls back to rebuilding the partition rather than publishing
// evidence it cannot vouch for.

// EvidenceRebindQueryer is the read surface ReboundCallEvidence needs; *sql.Tx
// satisfies it.
type EvidenceRebindQueryer interface {
	Queryer
	RowQueryer
}

// ReboundCallEvidence returns, for every persisted community whose evidence
// changes, its evidence list re-cited against the edges q holds now, encoded
// the way Persist encodes it.
func ReboundCallEvidence(ctx context.Context, q EvidenceRebindQueryer) (map[string]string, error) {
	calls, _, err := loadCertifiedCallEdges(ctx, q)
	if err != nil {
		return nil, err
	}
	byPair := make(map[[2]string][]int64)
	for _, e := range calls {
		k := [2]string{e.FileA, e.FileB}
		byPair[k] = append(byPair[k], e.ID)
	}
	for k := range byPair {
		ids := byPair[k]
		sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	}
	carried, err := Load(q)
	if err != nil {
		return nil, err
	}
	out := make(map[string]string)
	for _, c := range carried {
		rebound, err := reboundEvidence(c.EvidenceEdgeIDs, c.EvidenceTruncated, c.Members, byPair)
		if err != nil {
			return nil, fmt.Errorf("community %s: %w", c.ID, err)
		}
		if strings.Join(rebound, "\x00") == strings.Join(c.EvidenceEdgeIDs, "\x00") {
			continue
		}
		encoded, err := encodeStrings(rebound)
		if err != nil {
			return nil, err
		}
		out[c.ID] = encoded
	}
	return out, nil
}

// reboundEvidence rebuilds one community's evidence list in the order
// buildCommunity emits it: member pairs in (fileA, fileB) order, each pair's
// call ids ascending, then the pair's co-change marker when the carried list
// cited one. A truncated list is cut at the carried length.
func reboundEvidence(old []string, truncated bool, members []string, byPair map[[2]string][]int64) ([]string, error) {
	sorted := append([]string(nil), members...)
	sort.Strings(sorted)
	cochange := make(map[string]bool)
	for _, e := range old {
		if strings.HasPrefix(e, "cochange:") {
			cochange[e] = true
		}
	}
	var rebound []string
	for i := 0; i < len(sorted); i++ {
		for j := i + 1; j < len(sorted); j++ {
			for _, id := range byPair[[2]string{sorted[i], sorted[j]}] {
				rebound = append(rebound, strconv.FormatInt(id, 10))
			}
			if marker := "cochange:" + sorted[i] + "|" + sorted[j]; cochange[marker] {
				rebound = append(rebound, marker)
			}
		}
	}
	if truncated {
		if len(rebound) < len(old) {
			return nil, fmt.Errorf("truncated evidence has %d entries, current edges support %d", len(old), len(rebound))
		}
		rebound = rebound[:len(old)]
	}
	if len(rebound) != len(old) {
		return nil, fmt.Errorf("evidence shape changed: carried %d entries, current edges give %d", len(old), len(rebound))
	}
	for i := range old {
		oldMarker, newMarker := strings.HasPrefix(old[i], "cochange:"), strings.HasPrefix(rebound[i], "cochange:")
		if oldMarker != newMarker || (oldMarker && old[i] != rebound[i]) {
			return nil, fmt.Errorf("evidence position %d changed kind: %q -> %q", i, old[i], rebound[i])
		}
	}
	return rebound, nil
}

var _ EvidenceRebindQueryer = (*sql.Tx)(nil)

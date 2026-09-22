package main

// convergence_perfile_test.go — what a per-file (-file) amend must and may
// differ from a clean rebuild in. The per-file amend re-derives only the named
// files, so it CANNOT converge on every layer; it must instead (a) reproduce
// every layer it does re-derive exactly, (b) never serve a stale or dangling
// row, and (c) declare each layer it did not re-derive in project_meta
// (graph_convergence_state=per_file_partial + graph_convergence_gaps).

import (
	"fmt"
	"regexp"
	"strings"
)

// perFileOverlayCategories belong to the resolution overlay the per-file
// amend does not rebuild (gap resolution_overlay_absent, analysis_state
// not_run). Other files' surviving callsites are rebound in place, so these
// may differ in both directions.
var perFileOverlayCategories = map[string]bool{
	"resolution_symbols": true, "resolution_callsites": true, "resolution_candidates": true,
	"nodes.overlay": true, "edges.HAS_CALLSITE": true, "edges.CANDIDATE_TARGET": true,
	"edges.SELECTED_TARGET": true, "edges.HAS_DERIVATION_FACT": true,
	"edges.HAS_COMPLETENESS_FACT": true, "edges.HAS_UNRESOLVED_FACT": true,
}

// perFileDerivedCategories are deleted and marked not_run (gap
// derived_layers_not_run): they must be EMPTY, never stale.
var perFileDerivedCategories = map[string]bool{
	"closure": true, "cochanges": true, "communities": true, "community_members": true,
	"processes": true, "process_steps": true,
}

// perFileUneditedEdgeCategories may MISS rows (gap unedited_file_edge_rebinding:
// an edge whose source file was not re-resolved after its target appeared or
// was renamed), but may never hold a row the clean rebuild lacks.
var perFileUneditedEdgeCategories = map[string]bool{"edges.CALLS": true, "edges.IMPORTS": true}

var scopeStableIDField = regexp.MustCompile(`"scope_stable_id":"[0-9a-f]*",?`)

func perFileDivergences(clean, perFile graphSnapshot) []string {
	var out []string
	for cat, rows := range perFile {
		for _, r := range rows {
			if strings.Contains(r, "<dangling:") {
				out = append(out, fmt.Sprintf("%s holds a dangling row id: %s", cat, clip([]string{r})))
				break
			}
		}
	}
	meta := map[string]string{}
	for _, kv := range perFile["project_meta"] {
		if k, v, ok := strings.Cut(kv, "="); ok {
			meta[k] = v
		}
	}
	for key, want := range map[string]string{
		metaGraphConvergence: convergencePerFilePartial,
		"analysis_state":     "not_run",
	} {
		if meta[key] != want {
			out = append(out, fmt.Sprintf("project_meta[%s]=%q, want %q", key, meta[key], want))
		}
	}
	gaps := "," + meta[metaGraphConvergenceGaps] + ","
	for _, gap := range perFileConvergenceGaps {
		if !strings.Contains(gaps, ","+gap+",") {
			out = append(out, fmt.Sprintf("graph_convergence_gaps %q does not declare %s", meta[metaGraphConvergenceGaps], gap))
		}
	}
	for cat := range perFileDerivedCategories {
		if len(perFile[cat]) != 0 {
			out = append(out, fmt.Sprintf("%s serves %d rows the per-file amend could not re-derive", cat, len(perFile[cat])))
		}
	}
	for _, key := range []string{"derived_cochange_state", "derived_community_state", "derived_process_state"} {
		if meta[key] != "not_run" {
			out = append(out, fmt.Sprintf("project_meta[%s]=%q after a per-file amend, want not_run", key, meta[key]))
		}
	}
	for _, d := range snapshotDiff(clean, perFile, func(cat string) bool {
		return cat == "project_meta" || perFileOverlayCategories[cat] || perFileDerivedCategories[cat] ||
			perFileUneditedEdgeCategories[cat] || cat == "edges.READS" || cat == "edges.WRITES"
	}) {
		out = append(out, d)
	}
	// READS/WRITES: equal once the overlay-derived scope_stable_id is removed.
	strip := func(rows []string) []string {
		o := make([]string, len(rows))
		for i, r := range rows {
			o[i] = scopeStableIDField.ReplaceAllString(r, "")
		}
		return o
	}
	for _, cat := range []string{"edges.READS", "edges.WRITES"} {
		for _, d := range snapshotDiff(graphSnapshot{cat: strip(clean[cat])}, graphSnapshot{cat: strip(perFile[cat])}, nil) {
			out = append(out, d)
		}
	}
	for cat := range perFileUneditedEdgeCategories {
		if _, extra := multisetDiff(clean[cat], perFile[cat]); len(extra) > 0 {
			out = append(out, fmt.Sprintf("%s: per-file amend serves %d edge(s) a clean rebuild does not have (stale):%s", cat, len(extra), clip(extra)))
		}
	}
	return out
}

// perFileKnownGaps reports, for the log, the clean-only rows the declared
// gaps account for — the NOT CANONICAL-READY residue of the per-file path.
func perFileKnownGaps(clean, perFile graphSnapshot) []string {
	var out []string
	for cat := range perFileUneditedEdgeCategories {
		if missing, _ := multisetDiff(clean[cat], perFile[cat]); len(missing) > 0 {
			out = append(out, fmt.Sprintf("%s missing %d", cat, len(missing)))
		}
	}
	return out
}

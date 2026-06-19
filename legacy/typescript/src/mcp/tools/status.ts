/**
 * groundtruth_status — Health check + intervention stats.
 *
 * Returns LSP connection state, indexed symbol count, and stats summary.
 * The model can call this to check if GroundTruth is working and to
 * reference stats ("GroundTruth caught 58 hallucinations this week").
 *
 * Input:  {} (no input required)
 * Output: { lsp: string, indexed_symbols: number, stats: {} }
 */

import { LSPManager } from "../../lsp/manager.js";
import { SymbolGraph } from "../../symbol-graph/index.js";
import { InterventionTracker } from "../../stats/tracker.js";

export interface StatusOutput {
  lsp: string;
  indexed_symbols: number;
  stats: {
    total_validations: number;
    hallucinations_caught: number;
    fix_rate: string;
    estimated_time_saved: string;
  };
}

export function handleStatus(deps: {
  lspManager: LSPManager;
  symbolGraph: SymbolGraph;
  tracker: InterventionTracker;
}): StatusOutput {
  const stats = deps.tracker.getSummary(7); // last 7 days

  return {
    lsp: deps.lspManager.getStatus(),
    indexed_symbols: deps.symbolGraph.symbolCount,
    stats: {
      total_validations: stats.total,
      hallucinations_caught: stats.caught,
      fix_rate: stats.total > 0 ? `${stats.fixRate.toFixed(1)}%` : "N/A",
      estimated_time_saved: `${(stats.caught * 3 / 60).toFixed(1)} hours`,
    },
  };
}

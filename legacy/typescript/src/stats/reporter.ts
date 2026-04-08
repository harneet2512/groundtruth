/**
 * Stats Reporter — Formats intervention data for CLI output.
 *
 * Powers `groundtruth stats` CLI command. Queries the tracker
 * and formats results as human-readable terminal output or JSON.
 */

import { InterventionTracker } from "./tracker.js";

export function formatStats(
  tracker: InterventionTracker,
  options: { days?: number; json?: boolean; file?: string }
): string {
  const days = options.days ?? 7;
  const summary = tracker.getSummary(days);

  if (options.json) {
    return JSON.stringify(summary, null, 2);
  }

  const timeSaved = ((summary.caught * 3) / 60).toFixed(1);
  const catchRate =
    summary.total > 0
      ? ((summary.caught / summary.total) * 100).toFixed(1)
      : "0.0";

  const lines = [
    "",
    `  GroundTruth — Last ${days} Days`,
    "  " + "─".repeat(35),
    `  Total validations:          ${summary.total}`,
    `  Clean (no errors):          ${summary.clean}  (${(100 - parseFloat(catchRate)).toFixed(1)}%)`,
    `  Hallucinations caught:       ${summary.caught}  (${catchRate}%)`,
    `  Auto-fixed by AI:            ${summary.fixed}  (${summary.fixRate.toFixed(1)}% fix rate)`,
    "",
    "  By error type:",
  ];

  for (const [type, count] of Object.entries(summary.byType)) {
    const label = type.charAt(0).toUpperCase() + type.slice(1);
    lines.push(`    ${label} errors:${" ".repeat(Math.max(1, 16 - label.length))}${count}`);
  }

  lines.push("");
  lines.push(`  Estimated time saved:      ~${timeSaved} hours`);
  lines.push(`  Avg latency (clean):         ${summary.avgLatencyClean}ms`);
  lines.push(`  Avg latency (with fix):    ${summary.avgLatencyFix}ms`);
  lines.push("");

  return lines.join("\n");
}

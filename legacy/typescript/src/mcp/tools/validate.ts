/**
 * groundtruth_validate — Reactive post-write validation fallback.
 *
 * Called AFTER code is written to disk. Reads the file, validates all
 * symbols against LSP, and returns errors + fix suggestions.
 *
 * This is the safety net for when the model bypasses groundtruth_generate
 * and writes directly. Also used to infer ai_fix_accepted — if validate
 * runs on a file that generate already processed and finds zero errors,
 * the fix was accepted.
 *
 * Input:  { file_path: string }
 * Output: { valid: boolean, errors: [], suggested_fix: {} }
 */

import { LSPManager } from "../../lsp/manager.js";
import { SymbolGraph } from "../../symbol-graph/index.js";
import { InterventionTracker } from "../../stats/tracker.js";
import { validateCode } from "../../validator/index.js";
import { suggestFix } from "../../ai/fix-suggester.js";
import { readFile } from "fs/promises";

export interface ValidateInput {
  file_path: string;
}

export interface ValidateOutput {
  valid: boolean;
  errors: Array<{ symbol: string; reason: string }>;
  suggested_fix?: Record<string, string>;
}

export async function handleValidate(
  input: ValidateInput,
  deps: {
    lspManager: LSPManager;
    symbolGraph: SymbolGraph;
    tracker: InterventionTracker;
  }
): Promise<ValidateOutput> {
  const startTime = Date.now();

  // Read the file from disk
  const code = await readFile(input.file_path, "utf-8");

  // Validate against LSP
  const validation = await validateCode(code, deps.lspManager);

  // If errors, call AI for fix suggestions
  let suggested_fix: Record<string, string> | undefined;
  let aiCalled = false;

  if (validation.errors.length > 0) {
    aiCalled = true;
    const context = deps.symbolGraph.getRelevantContext("");
    suggested_fix = await suggestFix({
      intent: "",
      errors: validation.errors,
      availableSymbols: context.relevant_symbols,
    });
  }

  // Log intervention + infer ai_fix_accepted from prior generate call
  const latency = Date.now() - startTime;
  const priorGenerate = deps.tracker.getLastGenerateForFile(input.file_path);
  const aiFixAccepted =
    priorGenerate && priorGenerate.ai_called
      ? validation.errors.length === 0
      : null;

  // Update prior generate's ai_fix_accepted if applicable
  if (priorGenerate && aiFixAccepted !== null) {
    deps.tracker.updateAcceptance(priorGenerate.id, aiFixAccepted);
  }

  deps.tracker.log({
    tool: "validate",
    file_path: input.file_path,
    phase: "validation",
    outcome:
      validation.errors.length === 0
        ? "clean"
        : suggested_fix
          ? "fixed"
          : "caught",
    errors_found: validation.errors.length,
    errors_fixed: suggested_fix ? Object.keys(suggested_fix).length : 0,
    error_types: validation.errors.map((e) => e.type),
    latency_ms: latency,
    ai_called: aiCalled,
    fix_accepted: null,
  });

  return {
    valid: validation.errors.length === 0,
    errors: validation.errors.map((e) => ({
      symbol: e.symbol,
      reason: e.reason,
    })),
    suggested_fix,
  };
}

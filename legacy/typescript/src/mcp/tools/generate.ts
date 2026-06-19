/**
 * groundtruth_generate — The core proactive tool.
 *
 * The model calls this BEFORE writing code to disk. It:
 * 1. Parses proposed code via AST to extract imports/symbols
 * 2. Validates each symbol against the LSP (deterministic, <10ms)
 * 3. Queries the symbol graph for usage context
 * 4. If errors found: calls AI for fix suggestions (~500ms)
 * 5. Logs the intervention to the tracker
 *
 * Input:  { intent: string, proposed_code: string, file_path?: string }
 * Output: { valid: boolean, errors: [], suggested_fix: {}, context: {} }
 */

import { LSPManager } from "../../lsp/manager.js";
import { SymbolGraph } from "../../symbol-graph/index.js";
import { InterventionTracker } from "../../stats/tracker.js";
import { validateCode } from "../../validator/index.js";
import { suggestFix } from "../../ai/fix-suggester.js";

export interface GenerateInput {
  intent: string;
  proposed_code: string;
  file_path?: string;
}

export interface GenerateOutput {
  valid: boolean;
  errors: Array<{ symbol: string; reason: string }>;
  suggested_fix?: Record<string, string>;
  context?: {
    relevant_symbols: string[];
    usage_pattern?: string;
  };
}

export async function handleGenerate(
  input: GenerateInput,
  deps: {
    lspManager: LSPManager;
    symbolGraph: SymbolGraph;
    tracker: InterventionTracker;
  }
): Promise<GenerateOutput> {
  const startTime = Date.now();

  // 1. Validate proposed code against LSP
  const validation = await validateCode(input.proposed_code, deps.lspManager);

  // 2. Query symbol graph for context
  const context = deps.symbolGraph.getRelevantContext(input.intent);

  // 3. If errors, call AI for fix suggestions
  let suggested_fix: Record<string, string> | undefined;
  let aiCalled = false;

  if (validation.errors.length > 0) {
    aiCalled = true;
    suggested_fix = await suggestFix({
      intent: input.intent,
      errors: validation.errors,
      availableSymbols: context.relevant_symbols,
    });
  }

  // 4. Log intervention
  const latency = Date.now() - startTime;
  deps.tracker.log({
    tool: "generate",
    file_path: input.file_path ?? null,
    phase: "generate",
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
    fix_accepted: null, // inferred later via validate tool
  });

  return {
    valid: validation.errors.length === 0,
    errors: validation.errors.map((e) => ({
      symbol: e.symbol,
      reason: e.reason,
    })),
    suggested_fix,
    context: {
      relevant_symbols: context.relevant_symbols,
      usage_pattern: context.usage_pattern,
    },
  };
}

/**
 * MCP tool handlers — groundtruth_generate, groundtruth_validate, groundtruth_status,
 * groundtruth_find_relevant, groundtruth_trace_symbol.
 * Single file with Zod validation per CLAUDE/PRD.
 */
import { z } from 'zod';
import { readFile } from 'fs/promises';
import type { SymbolGraph } from '../symbol-graph/index.js';
import type { InterventionTracker } from '../stats/tracker.js';
import { GraphTraversal } from '../symbol-graph/graph-traversal.js';
import { validate } from '../validators/index.js';
import { generateBriefing } from '../ai/briefing-engine.js';
import { resolveSemantic } from '../ai/semantic-resolver.js';

const GenerateInputSchema = z.object({
  intent: z.string().optional(),
  proposed_code: z.string().optional(),
  file_path: z.string().optional(),
});

const ValidateInputSchema = z.object({
  file_path: z.string().min(1),
});

const FindRelevantInputSchema = z.object({
  description: z.string().min(1),
  entry_points: z.array(z.string()).optional(),
  max_files: z.number().optional(),
});

const TraceSymbolInputSchema = z.object({
  symbol: z.string().min(1),
  direction: z.enum(['callers', 'callees', 'both']).optional(),
  max_depth: z.number().optional(),
});

export type GenerateArgs = z.infer<typeof GenerateInputSchema>;
export type ValidateArgs = z.infer<typeof ValidateInputSchema>;
export type FindRelevantArgs = z.infer<typeof FindRelevantInputSchema>;
export type TraceSymbolArgs = z.infer<typeof TraceSymbolInputSchema>;

export interface ToolsDeps {
  symbolGraph: SymbolGraph;
  tracker: InterventionTracker;
}

export interface BriefingItem {
  name: string;
  kind: string;
  signature: string;
  file: string;
  usage_count: number;
}

export interface GenerateResponse {
  briefing?: {
    relevant_symbols: BriefingItem[];
    pattern: string;
    warnings: string[];
  };
  validation?: {
    valid: boolean;
    errors: Array<{
      type: 'symbol_not_found' | 'module_not_found' | 'package_not_installed' | 'signature_mismatch';
      symbol: string;
      location: string;
      message: string;
      fix?: { suggestion: string; source: 'levenshtein' | 'cross_index' | 'ai_semantic'; confidence: 'high' | 'medium' | 'low' };
    }>;
  };
  error?: string;
}

export interface ValidateResponse {
  valid: boolean;
  errors: Array<{
    type: string;
    symbol: string;
    location: string;
    message: string;
  }>;
}

export interface StatusResponse {
  indexed_symbols: number;
  stats: {
    total_validations: number;
    hallucinations_caught: number;
    fix_rate: string;
    estimated_time_saved: string;
  };
}

export interface FindRelevantResponse {
  files: Array<{
    path: string;
    relevance: 'high' | 'medium' | 'low';
    reason: string;
    symbols_involved: string[];
    distance: number;
  }>;
  entry_symbols: string[];
  graph_depth: number;
}

export interface TraceResponse {
  symbol: { name: string; file: string; signature: string };
  callers: Array<{ file: string; line: number; context: string }>;
  callees: Array<{ symbol: string; file: string }>;
  dependency_chain: string[];
  impact_radius: number;
}

export function validateGenerateArgs(args: unknown): { success: true; data: GenerateArgs } | { success: false; error: string } {
  const parsed = GenerateInputSchema.safeParse(args);
  if (!parsed.success) {
    return { success: false, error: parsed.error.message };
  }
  const { intent, proposed_code } = parsed.data;
  if (!intent && proposed_code === undefined) {
    return { success: false, error: 'Provide at least one of intent or proposed_code.' };
  }
  return { success: true, data: parsed.data };
}

export function validateValidateArgs(args: unknown): { success: true; data: ValidateArgs } | { success: false; error: string } {
  const parsed = ValidateInputSchema.safeParse(args);
  if (!parsed.success) {
    return { success: false, error: parsed.error.message };
  }
  return { success: true, data: parsed.data };
}

export function validateFindRelevantArgs(args: unknown): { success: true; data: FindRelevantArgs } | { success: false; error: string } {
  const parsed = FindRelevantInputSchema.safeParse(args);
  if (!parsed.success) {
    return { success: false, error: parsed.error.message };
  }
  return { success: true, data: parsed.data };
}

export function validateTraceSymbolArgs(args: unknown): { success: true; data: TraceSymbolArgs } | { success: false; error: string } {
  const parsed = TraceSymbolInputSchema.safeParse(args);
  if (!parsed.success) {
    return { success: false, error: parsed.error.message };
  }
  return { success: true, data: parsed.data };
}

export async function handleGenerate(args: GenerateArgs, deps: ToolsDeps): Promise<GenerateResponse> {
  const start = Date.now();
  const response: GenerateResponse = {};
  let errorsFound = 0;

  let aiCalled = false;

  if (args.intent) {
    const briefing = await generateBriefing(deps.symbolGraph.store, args.intent);
    response.briefing = briefing;
    aiCalled = !!process.env.OPENAI_API_KEY;
  }

  if (args.proposed_code !== undefined) {
    const result = validate(deps.symbolGraph.store, args.proposed_code, args.file_path ?? 'unknown');

    // Attempt AI semantic resolution for errors without fixes
    const enrichedErrors = await Promise.all(
      result.errors.map(async (e) => {
        if (e.fix || e.type === 'package_not_installed') {
          return { type: e.type, symbol: e.symbol, location: e.location, message: e.message, fix: e.fix };
        }
        const semanticFix = await resolveSemantic(
          deps.symbolGraph.store,
          { type: e.type, symbol: e.symbol, location: e.location, message: e.message },
          args.proposed_code!
        );
        if (semanticFix) aiCalled = true;
        return {
          type: e.type,
          symbol: e.symbol,
          location: e.location,
          message: e.message,
          fix: semanticFix ?? undefined,
        };
      })
    );

    response.validation = {
      valid: result.valid,
      errors: enrichedErrors,
    };
    errorsFound = result.errors.length;
  }

  const latency = Date.now() - start;
  const outcome = errorsFound > 0 ? 'caught' : 'clean';
  deps.tracker.log({
    tool: 'generate',
    file_path: args.file_path ?? null,
    phase: 'generate',
    outcome: outcome as 'clean' | 'caught' | 'fixed',
    errors_found: errorsFound,
    errors_fixed: 0,
    error_types: response.validation?.errors.map((e) => e.type) ?? [],
    latency_ms: latency,
    ai_called: aiCalled,
    ai_type: aiCalled ? (args.intent ? 'briefing' : 'semantic') : null,
    fix_accepted: null,
  });

  return response;
}

export async function handleValidate(args: ValidateArgs, deps: ToolsDeps): Promise<ValidateResponse> {
  const start = Date.now();
  let code: string;
  try {
    code = await readFile(args.file_path, 'utf-8');
  } catch {
    const latency = Date.now() - start;
    deps.tracker.log({
      tool: 'validate',
      file_path: args.file_path,
      phase: 'validation',
      outcome: 'caught',
      errors_found: 1,
      errors_fixed: 0,
      error_types: ['file_not_found'],
      latency_ms: latency,
      ai_called: false,
      fix_accepted: null,
    });
    return {
      valid: false,
      errors: [{ type: 'file_not_found', symbol: '', location: args.file_path, message: 'File not found.' }],
    };
  }

  const prior = deps.tracker.getLastGenerateForFile(args.file_path);
  const result = validate(deps.symbolGraph.store, code, args.file_path);
  const validation: ValidateResponse = {
    valid: result.valid,
    errors: result.errors.map((e) => ({
      type: e.type,
      symbol: e.symbol,
      location: e.location,
      message: e.message,
    })),
  };
  if (prior?.ai_called) {
    deps.tracker.updateAcceptance(prior.id, validation.valid);
  }

  const latency = Date.now() - start;
  deps.tracker.log({
    tool: 'validate',
    file_path: args.file_path,
    phase: 'validation',
    outcome: validation.valid ? 'clean' : 'caught',
    errors_found: validation.errors.length,
    errors_fixed: 0,
    error_types: validation.errors.map((e) => e.type),
    latency_ms: latency,
    ai_called: false,
    fix_accepted: null,
  });

  return validation;
}

export function handleStatus(deps: ToolsDeps): StatusResponse {
  const stats = deps.tracker.getSummary(7);
  return {
    indexed_symbols: deps.symbolGraph.symbolCount,
    stats: {
      total_validations: stats.total,
      hallucinations_caught: stats.caught,
      fix_rate: stats.total > 0 ? `${stats.fixRate.toFixed(1)}%` : 'N/A',
      estimated_time_saved: `${((stats.caught * 3) / 60).toFixed(1)} hours`,
    },
  };
}

export function handleFindRelevant(args: FindRelevantArgs, deps: ToolsDeps): FindRelevantResponse {
  const maxFiles = args.max_files ?? 10;
  const traversal = new GraphTraversal(deps.symbolGraph.store);
  const entryPoints = args.entry_points ?? [];
  let entrySymbols: string[] = [];
  let graphDepth = 0;
  let nodes: Array<{ path: string; distance: number }> = [];

  if (entryPoints.length > 0) {
    nodes = traversal.findConnectedFiles(entryPoints, 3);
    graphDepth = nodes.length > 0 ? Math.max(...nodes.map((n) => n.distance)) : 0;
  }

  const relevanceFromDistance = (d: number): 'high' | 'medium' | 'low' =>
    d === 0 ? 'high' : d === 1 ? 'medium' : 'low';

  const files = nodes
    .slice(0, maxFiles)
    .map((n) => ({
      path: n.path,
      relevance: relevanceFromDistance(n.distance),
      reason: n.distance === 0 ? 'entry point' : `${n.distance} hop(s) from entry in import graph`,
      symbols_involved: [] as string[],
      distance: n.distance,
    }));

  return {
    files,
    entry_symbols: entrySymbols,
    graph_depth: graphDepth,
  };
}

export function handleTraceSymbol(args: TraceSymbolArgs, deps: ToolsDeps): TraceResponse {
  const store = deps.symbolGraph.store;
  const traversal = new GraphTraversal(store);
  const direction = args.direction ?? 'both';

  const symbolRows = store.getSymbolByName(args.symbol);
  const defRow = symbolRows[0];
  const file = defRow ? defRow.file_path.replace(/\\/g, '/') : '';
  const exported = store.getExportedSymbolsByName(args.symbol);
  const signature = (exported[0]?.signature ?? '').trim() || '(no signature)';

  let callers: Array<{ file: string; line: number; context: string }> = [];
  if (direction === 'callers' || direction === 'both') {
    const refs = traversal.findCallers(args.symbol);
    callers = refs.map((r) => ({
      file: r.referenced_in_file.replace(/\\/g, '/'),
      line: r.referenced_at_line ?? 0,
      context: r.reference_type,
    }));
  }

  let callees: Array<{ symbol: string; file: string }> = [];
  if (defRow && (direction === 'callees' || direction === 'both')) {
    const path = file.endsWith('.ts') || file.endsWith('.tsx') ? file : `${file}.ts`;
    const imports = store.getImportsForFile(path);
    for (const imp of imports) {
      callees.push({
        symbol: imp.symbol_name,
        file: imp.symbol_file_path.replace(/\\/g, '/'),
      });
    }
    const inFileRefs = store.getReferencesInFile(path);
    for (const r of inFileRefs) {
      if (r.reference_type === 'call') {
        const sym = store.getSymbolById(r.symbol_id);
        if (sym) {
          callees.push({
            symbol: sym.name,
            file: sym.file_path.replace(/\\/g, '/'),
          });
        }
      }
    }
  }

  const { files: impactFiles } = traversal.getImpactRadius(args.symbol);
  const dependencyChain = [file, ...impactFiles.filter((f) => f !== file)];

  return {
    symbol: { name: args.symbol, file, signature },
    callers,
    callees,
    dependency_chain: [...new Set(dependencyChain.map((p) => p.replace(/\\/g, '/')))],
    impact_radius: impactFiles.length,
  };
}

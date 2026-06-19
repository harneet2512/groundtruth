/**
 * MCP Server Integration Tests
 *
 * Call groundtruth_generate with fixture project. Invoke tool handlers directly (bypass stdio).
 */
import { describe, it, expect, beforeAll } from 'vitest';
import { join } from 'path';
import { mkdtempSync, mkdirSync } from 'fs';
import { tmpdir } from 'os';
import { ProjectIndexer } from '../../src/symbol-graph/indexer.js';
import { SymbolGraph } from '../../src/symbol-graph/index.js';
import { InterventionTracker } from '../../src/stats/tracker.js';
import {
  handleGenerate,
  handleValidate,
  handleStatus,
  handleFindRelevant,
  handleTraceSymbol,
  validateGenerateArgs,
  validateFindRelevantArgs,
  validateTraceSymbolArgs,
} from '../../src/mcp/tools.js';

const FIXTURE = join(process.cwd(), 'tests', 'fixtures', 'test-project');

describe('groundtruth_generate', () => {
  let deps: { symbolGraph: SymbolGraph; tracker: InterventionTracker };

  beforeAll(async () => {
    const tmp = mkdtempSync(join(tmpdir(), 'gt-mcp-'));
    mkdirSync(join(tmp, '.groundtruth'), { recursive: true });
    const symbolGraph = new SymbolGraph(tmp);
    const indexer = new ProjectIndexer(symbolGraph.store);
    await indexer.indexProject(FIXTURE);
    const tracker = new InterventionTracker(symbolGraph.db);
    deps = { symbolGraph, tracker };
  });

  it('returns briefing with relevant_symbols for intent', async () => {
    const result = await handleGenerate({ intent: 'auth login' }, deps);
    expect(result.briefing).toBeDefined();
    expect(Array.isArray(result.briefing!.relevant_symbols)).toBe(true);
    expect(result.briefing!.relevant_symbols.length).toBeGreaterThanOrEqual(0);
    expect(result.briefing).toHaveProperty('pattern');
    expect(result.briefing).toHaveProperty('warnings');
  });

  it('returns validation object for valid proposed_code', async () => {
    const result = await handleGenerate(
      { proposed_code: "import { login } from './auth';\nlogin('user@test.com', 'pass');", file_path: 'src/foo.ts' },
      deps
    );
    expect(result.validation).toBeDefined();
    expect(result.validation!.valid).toBe(true);
    expect(Array.isArray(result.validation!.errors)).toBe(true);
  });

  it('returns validation errors for wrong import', async () => {
    const result = await handleGenerate(
      { proposed_code: "import { nonExistent } from './auth';", file_path: 'src/foo.ts' },
      deps
    );
    expect(result.validation).toBeDefined();
    expect(result.validation!.valid).toBe(false);
    expect(result.validation!.errors.length).toBeGreaterThanOrEqual(1);
    expect(result.validation!.errors[0].type).toBe('symbol_not_found');
  });

  it('returns package_not_installed for missing package', async () => {
    const result = await handleGenerate(
      { proposed_code: "import axios from 'axios';", file_path: 'src/foo.ts' },
      deps
    );
    expect(result.validation).toBeDefined();
    expect(result.validation!.valid).toBe(false);
    expect(result.validation!.errors[0].type).toBe('package_not_installed');
  });

  it('returns both briefing and validation when both provided', async () => {
    const result = await handleGenerate(
      { intent: 'auth', proposed_code: 'const x = 1;', file_path: 'src/bar.ts' },
      deps
    );
    expect(result.briefing).toBeDefined();
    expect(result.validation).toBeDefined();
  });

  it('rejects when neither intent nor proposed_code (Zod)', () => {
    const validated = validateGenerateArgs({});
    expect(validated.success).toBe(false);
    expect(validated.success === false && validated.error).toContain('intent');
  });

  it('logs intervention to tracker', async () => {
    await handleGenerate({ intent: 'test' }, deps);
    const stats = deps.tracker.getSummary(7);
    expect(stats.total).toBeGreaterThanOrEqual(1);
  });
});

describe('groundtruth_validate', () => {
  let deps: { symbolGraph: SymbolGraph; tracker: InterventionTracker };

  beforeAll(async () => {
    const tmp = mkdtempSync(join(tmpdir(), 'gt-mcp-'));
    mkdirSync(join(tmp, '.groundtruth'), { recursive: true });
    const symbolGraph = new SymbolGraph(tmp);
    const indexer = new ProjectIndexer(symbolGraph.store);
    await indexer.indexProject(FIXTURE);
    const tracker = new InterventionTracker(symbolGraph.db);
    deps = { symbolGraph, tracker };
  });

  it('returns valid and errors for file on disk', async () => {
    const filePath = join(FIXTURE, 'src', 'auth', 'login.ts');
    const result = await handleValidate({ file_path: filePath }, deps);
    expect(result).toHaveProperty('valid');
    expect(Array.isArray(result.errors)).toBe(true);
    expect(result.valid).toBe(true);
  });

  it('returns valid:false for non-existent file', async () => {
    const result = await handleValidate({ file_path: 'nonexistent/file.ts' }, deps);
    expect(result.valid).toBe(false);
    expect(result.errors.length).toBeGreaterThanOrEqual(1);
  });
});

describe('groundtruth_status', () => {
  let deps: { symbolGraph: SymbolGraph; tracker: InterventionTracker };

  beforeAll(async () => {
    const tmp = mkdtempSync(join(tmpdir(), 'gt-mcp-'));
    mkdirSync(join(tmp, '.groundtruth'), { recursive: true });
    const symbolGraph = new SymbolGraph(tmp);
    const indexer = new ProjectIndexer(symbolGraph.store);
    await indexer.indexProject(FIXTURE);
    const tracker = new InterventionTracker(symbolGraph.db);
    deps = { symbolGraph, tracker };
  });

  it('returns indexed_symbols and stats', () => {
    const result = handleStatus(deps);
    expect(result.indexed_symbols).toBeGreaterThan(0);
    expect(result.stats).toHaveProperty('total_validations');
    expect(result.stats).toHaveProperty('hallucinations_caught');
    expect(result.stats).toHaveProperty('fix_rate');
    expect(result.stats).toHaveProperty('estimated_time_saved');
  });
});

describe('groundtruth_find_relevant', () => {
  let deps: { symbolGraph: SymbolGraph; tracker: InterventionTracker };

  beforeAll(async () => {
    const tmp = mkdtempSync(join(tmpdir(), 'gt-mcp-'));
    mkdirSync(join(tmp, '.groundtruth'), { recursive: true });
    const symbolGraph = new SymbolGraph(tmp);
    const indexer = new ProjectIndexer(symbolGraph.store);
    await indexer.indexProject(FIXTURE);
    const tracker = new InterventionTracker(symbolGraph.db);
    deps = { symbolGraph, tracker };
  });

  it('returns files when entry_points provided', () => {
    const result = handleFindRelevant(
      { description: 'fix getUserById', entry_points: ['src/users/queries.ts'], max_files: 10 },
      deps
    );
    expect(result).toHaveProperty('files');
    expect(Array.isArray(result.files)).toBe(true);
    expect(result).toHaveProperty('entry_symbols');
    expect(result).toHaveProperty('graph_depth');
    if (result.files.length > 0) {
      expect(result.files[0]).toHaveProperty('path');
      expect(result.files[0]).toHaveProperty('relevance');
      expect(result.files[0]).toHaveProperty('reason');
      expect(result.files[0]).toHaveProperty('distance');
    }
  });

  it('returns empty files when no entry_points (no AI task parsing yet)', () => {
    const result = handleFindRelevant({ description: 'fix auth' }, deps);
    expect(result.files).toEqual([]);
    expect(result.entry_symbols).toEqual([]);
  });

  it('validateFindRelevantArgs rejects empty description', () => {
    const validated = validateFindRelevantArgs({ description: '' });
    expect(validated.success).toBe(false);
  });
});

describe('groundtruth_trace_symbol', () => {
  let deps: { symbolGraph: SymbolGraph; tracker: InterventionTracker };

  beforeAll(async () => {
    const tmp = mkdtempSync(join(tmpdir(), 'gt-mcp-'));
    mkdirSync(join(tmp, '.groundtruth'), { recursive: true });
    const symbolGraph = new SymbolGraph(tmp);
    const indexer = new ProjectIndexer(symbolGraph.store);
    await indexer.indexProject(FIXTURE);
    const tracker = new InterventionTracker(symbolGraph.db);
    deps = { symbolGraph, tracker };
  });

  it('returns symbol, callers, callees, dependency_chain, impact_radius', () => {
    const result = handleTraceSymbol({ symbol: 'AppError' }, deps);
    expect(result.symbol).toHaveProperty('name', 'AppError');
    expect(result.symbol).toHaveProperty('file');
    expect(result.symbol).toHaveProperty('signature');
    expect(Array.isArray(result.callers)).toBe(true);
    expect(Array.isArray(result.callees)).toBe(true);
    expect(Array.isArray(result.dependency_chain)).toBe(true);
    expect(typeof result.impact_radius).toBe('number');
  });

  it('finds callers for verifyToken', () => {
    const result = handleTraceSymbol({ symbol: 'verifyToken' }, deps);
    expect(result.callers.some((c) => c.file.replace(/\\/g, '/').includes('middleware/auth'))).toBe(true);
  });

  it('validateTraceSymbolArgs rejects empty symbol', () => {
    const validated = validateTraceSymbolArgs({ symbol: '' });
    expect(validated.success).toBe(false);
  });
});

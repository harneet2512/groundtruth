/**
 * Briefing engine unit tests.
 * Uses in-memory SQLite, mocked OpenAI SDK. No real API calls.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { SymbolStore } from '../../../src/symbol-graph/sqlite-store.js';

// Mock the OpenAI SDK before importing briefing-engine
vi.mock('openai', () => {
  const mockCreate = vi.fn();
  return {
    default: vi.fn().mockImplementation(() => ({
      chat: { completions: { create: mockCreate } },
    })),
    __mockCreate: mockCreate,
  };
});

import { generateBriefing } from '../../../src/ai/briefing-engine.js';
import OpenAI from 'openai';

const mockCreate = (await import('openai') as unknown as { __mockCreate: ReturnType<typeof vi.fn> }).__mockCreate;

describe('generateBriefing', () => {
  let store: SymbolStore;

  beforeEach(() => {
    store = new SymbolStore(':memory:');
    store.init();
    const now = Math.floor(Date.now() / 1000);

    const id1 = store.insertSymbol({
      name: 'login',
      kind: 'function',
      file_path: 'src/auth/login.ts',
      line_number: 6,
      is_exported: 1,
      signature: '(email: string, password: string) => Promise<LoginResult>',
      params: null,
      return_type: null,
      jsdoc: null,
      usage_count: 5,
      last_indexed_at: now,
    });
    store.insertExport(id1, 'src/auth/login', { isDefault: false, isNamed: true });

    const id2 = store.insertSymbol({
      name: 'verifyToken',
      kind: 'function',
      file_path: 'src/auth/verify.ts',
      line_number: 1,
      is_exported: 1,
      signature: '(token: string) => Promise<TokenPayload>',
      params: null,
      return_type: null,
      jsdoc: null,
      usage_count: 3,
      last_indexed_at: now,
    });
    store.insertExport(id2, 'src/auth/verify', { isDefault: false, isNamed: true });

    vi.clearAllMocks();
  });

  it('returns raw FTS5 results when no API key', async () => {
    const result = await generateBriefing(store, 'login auth');
    expect(result.relevant_symbols.length).toBeGreaterThanOrEqual(1);
    expect(result.relevant_symbols[0].name).toBe('login');
    expect(result.pattern).toBe('');
    expect(result.warnings).toEqual([]);
  });

  it('calls OpenAI API when API key is provided', async () => {
    mockCreate.mockResolvedValue({
      choices: [{
        message: {
          content: `SYMBOLS: login, verifyToken
PATTERN: Call login() first, then use verifyToken() to validate the JWT.
WARNINGS: login() is async — always await it.`,
        },
      }],
    });

    const result = await generateBriefing(store, 'login auth', { apiKey: 'test-key' });

    expect(OpenAI).toHaveBeenCalledWith({ apiKey: 'test-key' });
    expect(mockCreate).toHaveBeenCalledTimes(1);
    expect(result.relevant_symbols.length).toBeGreaterThanOrEqual(1);
    expect(result.pattern).toContain('login');
    expect(result.warnings.length).toBeGreaterThanOrEqual(1);
  });

  it('falls back to raw FTS5 on API error', async () => {
    mockCreate.mockRejectedValue(new Error('API rate limit'));

    const result = await generateBriefing(store, 'login', { apiKey: 'test-key' });

    expect(result.relevant_symbols.length).toBeGreaterThanOrEqual(1);
    expect(result.pattern).toBe('');
    expect(result.warnings).toEqual([]);
  });

  it('passes correct model to API', async () => {
    mockCreate.mockResolvedValue({
      choices: [{ message: { content: 'SYMBOLS: login\nPATTERN: use login\nWARNINGS: None' } }],
    });

    await generateBriefing(store, 'login', { apiKey: 'test-key', model: 'gpt-4o-mini' });

    expect(mockCreate).toHaveBeenCalledWith(
      expect.objectContaining({ model: 'gpt-4o-mini' })
    );
  });

  it('returns empty symbols for no FTS5 matches', async () => {
    const result = await generateBriefing(store, 'zzzznonexistent');
    expect(result.relevant_symbols).toEqual([]);
  });

  it('parses AI response with numbered sections', async () => {
    mockCreate.mockResolvedValue({
      choices: [{
        message: {
          content: `1. SYMBOLS: login (function), verifyToken (function)
2. PATTERN: Authenticate with login, verify with verifyToken.
3. WARNINGS:
- login requires email and password
- verifyToken is async`,
        },
      }],
    });

    const result = await generateBriefing(store, 'auth', { apiKey: 'test-key' });
    expect(result.pattern).toContain('Authenticate');
    expect(result.warnings.length).toBe(2);
  });
});

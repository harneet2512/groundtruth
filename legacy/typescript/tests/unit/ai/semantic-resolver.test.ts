/**
 * Semantic resolver unit tests.
 * Uses in-memory SQLite, mocked OpenAI SDK. No real API calls.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { SymbolStore } from '../../../src/symbol-graph/sqlite-store.js';

// Mock the OpenAI SDK
vi.mock('openai', () => {
  const mockCreate = vi.fn();
  return {
    default: vi.fn().mockImplementation(() => ({
      chat: { completions: { create: mockCreate } },
    })),
    __mockCreate: mockCreate,
  };
});

import { resolveSemantic } from '../../../src/ai/semantic-resolver.js';

const mockCreate = (await import('openai') as unknown as { __mockCreate: ReturnType<typeof vi.fn> }).__mockCreate;

describe('resolveSemantic', () => {
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

    vi.clearAllMocks();
  });

  it('returns null when no API key', async () => {
    const result = await resolveSemantic(
      store,
      { type: 'symbol_not_found', symbol: 'signin', location: 'src/foo.ts', message: 'not found' },
      'import { signin } from "./auth";'
    );
    expect(result).toBeNull();
  });

  it('calls OpenAI API and returns fix', async () => {
    mockCreate.mockResolvedValue({
      choices: [{
        message: {
          content: '{"suggestion": "Use \'login\' instead of \'signin\'", "confidence": "high"}',
        },
      }],
    });

    const result = await resolveSemantic(
      store,
      { type: 'symbol_not_found', symbol: 'signin', location: 'src/foo.ts', message: 'not found' },
      'import { signin } from "./auth";',
      { apiKey: 'test-key' }
    );

    expect(result).not.toBeNull();
    expect(result!.source).toBe('ai_semantic');
    expect(result!.suggestion).toContain('login');
    expect(result!.confidence).toBe('high');
  });

  it('returns null on API error', async () => {
    mockCreate.mockRejectedValue(new Error('API error'));

    const result = await resolveSemantic(
      store,
      { type: 'symbol_not_found', symbol: 'signin', location: 'src/foo.ts', message: 'not found' },
      'code',
      { apiKey: 'test-key' }
    );

    expect(result).toBeNull();
  });

  it('returns null for unparseable AI response', async () => {
    mockCreate.mockResolvedValue({
      choices: [{ message: { content: 'I cannot determine the fix.' } }],
    });

    const result = await resolveSemantic(
      store,
      { type: 'symbol_not_found', symbol: 'x', location: 'y', message: 'z' },
      'code',
      { apiKey: 'test-key' }
    );

    expect(result).toBeNull();
  });

  it('handles JSON wrapped in markdown code block', async () => {
    mockCreate.mockResolvedValue({
      choices: [{
        message: {
          content: '```json\n{"suggestion": "Use login()", "confidence": "medium"}\n```',
        },
      }],
    });

    const result = await resolveSemantic(
      store,
      { type: 'symbol_not_found', symbol: 'signin', location: 'src/foo.ts', message: 'not found' },
      'code',
      { apiKey: 'test-key' }
    );

    expect(result).not.toBeNull();
    expect(result!.confidence).toBe('medium');
  });

  it('defaults confidence to low for unknown values', async () => {
    mockCreate.mockResolvedValue({
      choices: [{
        message: {
          content: '{"suggestion": "Try something", "confidence": "very_high"}',
        },
      }],
    });

    const result = await resolveSemantic(
      store,
      { type: 'symbol_not_found', symbol: 'x', location: 'y', message: 'z' },
      'code',
      { apiKey: 'test-key' }
    );

    expect(result).not.toBeNull();
    expect(result!.confidence).toBe('low');
  });

  it('uses correct model', async () => {
    mockCreate.mockResolvedValue({
      choices: [{ message: { content: '{"suggestion": "fix", "confidence": "high"}' } }],
    });

    await resolveSemantic(
      store,
      { type: 'symbol_not_found', symbol: 'x', location: 'y', message: 'z' },
      'code',
      { apiKey: 'test-key', model: 'gpt-4o-mini' }
    );

    expect(mockCreate).toHaveBeenCalledWith(
      expect.objectContaining({ model: 'gpt-4o-mini', max_tokens: 100 })
    );
  });
});

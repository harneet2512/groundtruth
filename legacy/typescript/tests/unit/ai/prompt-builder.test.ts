/**
 * Prompt builder unit tests.
 */
import { describe, it, expect } from 'vitest';
import { buildBriefingPrompt, buildSemanticPrompt } from '../../../src/ai/prompt-builder.js';

describe('buildBriefingPrompt', () => {
  it('includes intent in the prompt', () => {
    const prompt = buildBriefingPrompt('add JWT auth middleware', []);
    expect(prompt).toContain('add JWT auth middleware');
  });

  it('includes symbol details', () => {
    const prompt = buildBriefingPrompt('auth', [
      { name: 'login', kind: 'function', signature: '(email: string) => void', file_path: 'src/auth.ts', usage_count: 5 },
    ]);
    expect(prompt).toContain('login');
    expect(prompt).toContain('function');
    expect(prompt).toContain('src/auth.ts');
    expect(prompt).toContain('used 5x');
  });

  it('handles empty symbols list', () => {
    const prompt = buildBriefingPrompt('something', []);
    expect(prompt).toContain('no matching symbols found');
  });

  it('requests concise output format', () => {
    const prompt = buildBriefingPrompt('test', []);
    expect(prompt).toContain('SYMBOLS');
    expect(prompt).toContain('PATTERN');
    expect(prompt).toContain('WARNINGS');
  });

  it('stays under ~800 tokens for reasonable input', () => {
    const symbols = Array.from({ length: 10 }, (_, i) => ({
      name: `func${i}`,
      kind: 'function',
      signature: `(x: number) => number`,
      file_path: `src/mod${i}.ts`,
      usage_count: i,
    }));
    const prompt = buildBriefingPrompt('some intent', symbols);
    // Rough token estimate: ~4 chars per token
    expect(prompt.length).toBeLessThan(3200); // ~800 tokens
  });
});

describe('buildSemanticPrompt', () => {
  it('includes error description', () => {
    const prompt = buildSemanticPrompt(
      'symbol_not_found: login not exported from ./auth',
      'import { login } from "./auth";',
      ['login (function) — src/auth/login.ts']
    );
    expect(prompt).toContain('symbol_not_found');
    expect(prompt).toContain('login');
  });

  it('includes code context', () => {
    const context = 'import { foo } from "./bar";';
    const prompt = buildSemanticPrompt('error', context, []);
    expect(prompt).toContain(context);
  });

  it('includes related symbols', () => {
    const prompt = buildSemanticPrompt('error', 'code', ['validateEmail — src/utils.ts']);
    expect(prompt).toContain('validateEmail');
  });

  it('handles no related symbols', () => {
    const prompt = buildSemanticPrompt('error', 'code', []);
    expect(prompt).toContain('none found');
  });

  it('requests JSON output', () => {
    const prompt = buildSemanticPrompt('error', 'code', []);
    expect(prompt).toContain('JSON');
    expect(prompt).toContain('suggestion');
    expect(prompt).toContain('confidence');
  });
});

/**
 * Briefing engine — intent → FTS5 query → LLM distills → briefing.
 * Falls back to raw FTS5 results if no API key.
 */
import OpenAI from 'openai';
import type { SymbolStore } from '../symbol-graph/sqlite-store.js';
import { searchSymbolsSemantic } from '../symbol-graph/query.js';
import { buildBriefingPrompt } from './prompt-builder.js';
import { DEFAULT_MODEL } from './client.js';
import { logger } from '../utils/logger.js';

export interface BriefingSymbol {
  name: string;
  kind: string;
  signature: string;
  file: string;
  usage_count: number;
}

export interface Briefing {
  relevant_symbols: BriefingSymbol[];
  pattern: string;
  warnings: string[];
}

export interface BriefingEngineOptions {
  apiKey?: string;
  model?: string;
}

function parseAiBriefing(text: string, symbols: BriefingSymbol[]): Briefing {
  let pattern = '';
  const warnings: string[] = [];

  const lines = text.split('\n');
  let section: 'none' | 'symbols' | 'pattern' | 'warnings' = 'none';

  for (const line of lines) {
    const trimmed = line.trim();
    const upper = trimmed.toUpperCase();

    if (upper.startsWith('SYMBOLS:') || upper.startsWith('1.') || upper.startsWith('**SYMBOLS')) {
      section = 'symbols';
      continue;
    }
    if (upper.startsWith('PATTERN:') || upper.startsWith('2.') || upper.startsWith('**PATTERN')) {
      section = 'pattern';
      const after = trimmed.replace(/^\*?\*?PATTERN\*?\*?:?\s*/i, '').replace(/^2\.\s*(?:PATTERN:?\s*)?/i, '');
      if (after) pattern = after;
      continue;
    }
    if (upper.startsWith('WARNINGS:') || upper.startsWith('3.') || upper.startsWith('**WARNINGS')) {
      section = 'warnings';
      const after = trimmed.replace(/^\*?\*?WARNINGS\*?\*?:?\s*/i, '').replace(/^3\.\s*(?:WARNINGS:?\s*)?/i, '');
      if (after && after !== 'None' && after !== 'None.') warnings.push(after);
      continue;
    }

    if (section === 'pattern' && trimmed) {
      pattern = pattern ? `${pattern} ${trimmed}` : trimmed;
    }
    if (section === 'warnings' && trimmed && trimmed !== '-' && trimmed !== 'None' && trimmed !== 'None.') {
      warnings.push(trimmed.replace(/^[-•*]\s*/, ''));
    }
  }

  return { relevant_symbols: symbols, pattern, warnings };
}

/**
 * Generate a briefing for the given intent.
 * Queries FTS5 for matching symbols, then calls LLM to distill.
 * Falls back to raw FTS5 results if no API key or on API error.
 */
export async function generateBriefing(
  store: SymbolStore,
  intent: string,
  options?: BriefingEngineOptions
): Promise<Briefing> {
  const matchingSymbols = searchSymbolsSemantic(store, intent);
  const symbols: BriefingSymbol[] = matchingSymbols.map((s) => ({
    name: s.name,
    kind: s.kind,
    signature: s.signature ?? '',
    file: s.file_path,
    usage_count: s.usage_count,
  }));

  const apiKey = options?.apiKey ?? process.env.OPENAI_API_KEY;
  if (!apiKey) {
    logger.debug('No OPENAI_API_KEY — returning raw FTS5 results');
    return { relevant_symbols: symbols, pattern: '', warnings: [] };
  }

  const prompt = buildBriefingPrompt(
    intent,
    matchingSymbols.map((s) => ({
      name: s.name,
      kind: s.kind,
      signature: s.signature,
      file_path: s.file_path,
      usage_count: s.usage_count,
    }))
  );

  try {
    const client = new OpenAI({ apiKey });
    const model = options?.model ?? DEFAULT_MODEL;
    const response = await client.chat.completions.create({
      model,
      max_tokens: 200,
      messages: [{ role: 'user', content: prompt }],
    });

    const text = response.choices[0]?.message?.content ?? '';
    return parseAiBriefing(text, symbols);
  } catch (err) {
    logger.warn('AI briefing failed, falling back to raw FTS5 results', err);
    return { relevant_symbols: symbols, pattern: '', warnings: [] };
  }
}

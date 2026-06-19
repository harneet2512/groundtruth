/**
 * Semantic resolver — AI fallback when deterministic + Levenshtein + cross-index all fail.
 * Sends error + context + related symbols to LLM → returns fix.
 * Falls back gracefully if no API key or on error.
 */
import OpenAI from 'openai';
import type { SymbolStore } from '../symbol-graph/sqlite-store.js';
import { searchSymbolsSemantic } from '../symbol-graph/query.js';
import { buildSemanticPrompt } from './prompt-builder.js';
import { DEFAULT_MODEL } from './client.js';
import { logger } from '../utils/logger.js';

export interface SemanticFix {
  suggestion: string;
  source: 'ai_semantic';
  confidence: 'high' | 'medium' | 'low';
}

export interface ValidationError {
  type: string;
  symbol: string;
  location: string;
  message: string;
}

export interface SemanticResolverOptions {
  apiKey?: string;
  model?: string;
}

/**
 * Attempt AI semantic resolution for a validation error.
 * Only called when deterministic + Levenshtein + cross-index all failed.
 * Returns null if no API key, on error, or if AI can't determine a fix.
 */
export async function resolveSemantic(
  store: SymbolStore,
  error: ValidationError,
  context: string,
  options?: SemanticResolverOptions
): Promise<SemanticFix | null> {
  const apiKey = options?.apiKey ?? process.env.OPENAI_API_KEY;
  if (!apiKey) {
    logger.debug('No OPENAI_API_KEY — skipping semantic resolution');
    return null;
  }

  // Search for related symbols to give the AI context
  const related = searchSymbolsSemantic(store, error.symbol);
  const relatedSymbols = related.slice(0, 10).map((s) => {
    const sig = s.signature ? `: ${s.signature}` : '';
    return `${s.name} (${s.kind})${sig} — ${s.file_path}`;
  });

  const errorDesc = `${error.type}: ${error.message} (symbol: "${error.symbol}" at ${error.location})`;
  const prompt = buildSemanticPrompt(errorDesc, context, relatedSymbols);

  try {
    const client = new OpenAI({ apiKey });
    const model = options?.model ?? DEFAULT_MODEL;
    const response = await client.chat.completions.create({
      model,
      max_tokens: 100,
      messages: [{ role: 'user', content: prompt }],
    });

    const text = response.choices[0]?.message?.content ?? '';
    return parseSemanticResponse(text);
  } catch (err) {
    logger.warn('AI semantic resolution failed', err);
    return null;
  }
}

function parseSemanticResponse(text: string): SemanticFix | null {
  try {
    // Extract JSON from response (may be wrapped in markdown code block)
    const jsonMatch = text.match(/\{[\s\S]*\}/);
    if (!jsonMatch) return null;

    const parsed = JSON.parse(jsonMatch[0]) as { suggestion?: string; confidence?: string };
    if (!parsed.suggestion) return null;

    const confidence = (['high', 'medium', 'low'] as const).includes(
      parsed.confidence as 'high' | 'medium' | 'low'
    )
      ? (parsed.confidence as 'high' | 'medium' | 'low')
      : 'low';

    return {
      suggestion: parsed.suggestion,
      source: 'ai_semantic',
      confidence,
    };
  } catch {
    return null;
  }
}

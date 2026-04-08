/**
 * Prompt builder — shared by briefing engine and semantic resolver.
 * Builds tight, specific prompts with just the error/intent + relevant symbols.
 */

export interface SymbolContext {
  name: string;
  kind: string;
  signature: string | null;
  file_path: string;
  usage_count: number;
}

/**
 * Build a briefing prompt (~500-800 tokens in, ~150 tokens out).
 * Intent + matching symbols → Haiku distills into actionable briefing.
 */
export function buildBriefingPrompt(
  intent: string,
  symbols: SymbolContext[]
): string {
  const symbolList = symbols
    .map((s) => {
      const sig = s.signature ? `: ${s.signature}` : '';
      return `- ${s.name} (${s.kind})${sig} — ${s.file_path} [used ${s.usage_count}x]`;
    })
    .join('\n');

  return `You are a codebase assistant. A developer wants to: "${intent}"

Here are the relevant exported symbols from the codebase:
${symbolList || '(no matching symbols found)'}

Respond with a concise briefing (under 150 tokens) containing:
1. SYMBOLS: List the most relevant symbols for this task (name, file, signature)
2. PATTERN: How these symbols are typically used together (one sentence)
3. WARNINGS: Any pitfalls — required params, async functions, type constraints

Be direct. No preamble. Use the exact symbol names and file paths from above.`;
}

/**
 * Build a semantic resolution prompt (~300 tokens in, ~50 tokens out).
 * Error + context + related symbols → Haiku returns fix.
 */
export function buildSemanticPrompt(
  error: string,
  context: string,
  relatedSymbols: string[]
): string {
  const symbolList = relatedSymbols.length > 0
    ? relatedSymbols.map((s) => `- ${s}`).join('\n')
    : '(none found)';

  return `A code validator found this error:
${error}

Code context:
${context}

Related symbols in the codebase:
${symbolList}

What is the correct fix? Respond with ONLY a JSON object:
{"suggestion": "brief fix description", "confidence": "high|medium|low"}`;
}

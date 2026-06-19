/**
 * AI Prompt Templates — The exact prompts sent to Haiku.
 *
 * Kept in one file so they're easy to audit, test, and iterate on.
 * Every claim in the AI response must be grounded in the available
 * symbols list — the model can't invent suggestions.
 */

import { FixSuggesterInput } from "./fix-suggester.js";

export function buildFixPrompt(input: FixSuggesterInput): string {
  const errorList = input.errors
    .map((e) => `- ${e.symbol}: ${e.reason}`)
    .join("\n");

  const symbolList = input.availableSymbols.join("\n");

  return `You are a code fix assistant. The developer's intent is: "${input.intent}"

The following symbols were used but do not exist in the codebase:
${errorList}

These symbols ARE available in the codebase:
${symbolList}

For each incorrect symbol, suggest the best replacement from the available symbols.
Consider the developer's intent when choosing — pick the symbol that best matches
what they're trying to do, not just the closest string match.

Respond in JSON format only:
{
  "fixes": {
    "<wrong_symbol>": "<correct_symbol_with_signature>"
  }
}

IMPORTANT: Only suggest symbols from the available list above. Do not invent symbols.`;
}

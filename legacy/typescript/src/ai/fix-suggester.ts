/**
 * AI Fix Suggester — Calls Haiku when validation catches errors.
 *
 * ONLY fires when the deterministic validator finds concrete errors.
 * Never fires on the happy path. This is the entire AI layer.
 *
 * Input: the wrong symbol, the available symbols, and the user's intent.
 * Output: a mapping of wrong_symbol → correct_symbol.
 *
 * Why AI and not fuzzy string matching?
 * - "authenticate" could map to login(), verifyToken(), or createSession()
 *   depending on intent. Levenshtein distance picks the wrong one.
 * - AI reads the intent ("add auth middleware") and picks verifyToken()
 *   because that's what middleware uses for token validation.
 *
 * Cost: ~$0.001 per call. Only on errors. Typically 2-5 calls per day.
 */

import { createClient } from "./client.js";
import { buildFixPrompt } from "./prompts.js";

export interface FixSuggesterInput {
  intent: string;
  errors: Array<{ symbol: string; type: string; reason: string }>;
  availableSymbols: string[];
}

export async function suggestFix(
  input: FixSuggesterInput
): Promise<Record<string, string>> {
  if (input.errors.length === 0) return {};

  const client = createClient();
  const prompt = buildFixPrompt(input);

  // TODO: Call Haiku with the prompt
  // TODO: Parse response into symbol mapping
  // TODO: Return { "authenticate": "login(credentials: LoginCredentials)" }

  return {};
}

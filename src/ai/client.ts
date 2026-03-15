/**
 * OpenAI SDK Client — Thin wrapper for API calls.
 *
 * Creates a single OpenAI client instance. Uses gpt-4o-mini by default
 * for cost efficiency (~$0.001 per fix suggestion call).
 */

import OpenAI from 'openai';

let client: OpenAI | null = null;

export function createClient(apiKey?: string): OpenAI {
  if (!client) {
    client = new OpenAI({ apiKey });
    // Uses OPENAI_API_KEY from environment if apiKey not provided
  }
  return client;
}

export const DEFAULT_MODEL = 'gpt-4o-mini';

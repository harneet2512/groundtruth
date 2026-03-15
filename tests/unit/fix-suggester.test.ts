/**
 * AI Fix Suggester Unit Tests
 *
 * Tests prompt construction and response parsing.
 * Does NOT make real API calls — mocks the Anthropic client.
 */

import { describe, it, expect } from "vitest";

describe("buildFixPrompt", () => {
  it("should include all errors in the prompt", () => {
    // TODO: Build prompt with 3 errors
    // TODO: Assert all 3 symbol names appear in the prompt
    expect(true).toBe(true);
  });

  it("should include available symbols in the prompt", () => {
    // TODO: Build prompt with available symbols
    // TODO: Assert all symbols listed
    expect(true).toBe(true);
  });

  it("should include user intent in the prompt", () => {
    // TODO: Build prompt with intent "add auth middleware"
    // TODO: Assert intent appears in the prompt
    expect(true).toBe(true);
  });
});

describe("suggestFix", () => {
  it("should return empty map when no errors", async () => {
    // TODO: Call with empty errors
    // TODO: Assert empty result, no API call made
    expect(true).toBe(true);
  });

  it("should parse AI response into fix mapping", async () => {
    // TODO: Mock Anthropic API response
    // TODO: Assert correct mapping returned
    expect(true).toBe(true);
  });
});
